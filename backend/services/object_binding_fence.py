"""DB-clock binding leases that protect formal object writes before binding."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text

from services.purge_object_fence import ObjectIdentity


class BindingFenceHeld(RuntimeError):
    error_code = 'PURGE_CONCURRENT_REFERENCE_BLOCKED'


@dataclass(frozen=True)
class BindingFenceLease:
    owner_token: uuid.UUID
    owner_generation: int
    fence_ids: tuple[uuid.UUID, ...]
    identities: tuple[ObjectIdentity, ...]


class ObjectBindingFenceService:
    def __init__(self, session, *, purge_fence_service=None):
        self.session = session
        self._purge_fence_service = purge_fence_service

    @staticmethod
    def _canonical(identities):
        return tuple(sorted(set(identities)))

    @staticmethod
    def sublease(lease: BindingFenceLease, identities) -> BindingFenceLease:
        """Subset view of a chunk lease sharing the parent owner token/generation.

        ``acquire`` 生成的 ``fence_ids`` 与 canonical ``identities`` 平行，子租约
        仅引用父租约中的一组身份，用于 chunk-owner 的逐 item 释放。
        """
        wanted = ObjectBindingFenceService._canonical(identities)
        wanted_set = set(wanted)
        selected = [
            (identity, fence_id)
            for identity, fence_id in zip(lease.identities, lease.fence_ids)
            if identity in wanted_set
        ]
        if len(selected) != len(wanted):
            raise ValueError('子租约身份必须是父租约 identity 集的子集')
        return BindingFenceLease(
            owner_token=lease.owner_token,
            owner_generation=lease.owner_generation,
            fence_ids=tuple(fence_id for _, fence_id in selected),
            identities=tuple(identity for identity, _ in selected),
        )

    def acquire(self, identities, *, owner_kind: str, lease_seconds: int) -> BindingFenceLease:
        from models import ObjectBindingFence

        canonical = self._canonical(identities)
        if not canonical or lease_seconds <= 0:
            raise ValueError('binding fence 参数无效')
        token = uuid.uuid4()
        try:
            with self.session.begin():
                self._lock_all(canonical)
                if self._purge_fence_service is not None:
                    try:
                        self._purge_fence_service.assert_bindable(canonical)
                    except Exception as exc:
                        if getattr(exc, 'error_code', None) == 'PURGE_OBJECT_FENCE_HELD':
                            raise BindingFenceHeld('正式对象正处于永久清除围栏中') from None
                        raise
                now = self._clock()
                created = []
                generation = 1
                for identity in canonical:
                    held = self.session.execute(
                        select(ObjectBindingFence)
                        .where(
                            ObjectBindingFence.formal_bucket == identity.formal_bucket,
                            ObjectBindingFence.formal_key == identity.formal_key,
                            ObjectBindingFence.state == 'held',
                        ).with_for_update()
                    ).scalar_one_or_none()
                    if held is not None and held.lease_expires_at > now:
                        raise BindingFenceHeld('正式对象存在有效绑定租约')
                    if held is not None:
                        generation = max(generation, held.owner_generation + 1)
                        held.state = 'released'
                        held.released_at = now
                        held.release_reason = 'lease_expired'
                    fence = ObjectBindingFence(
                        formal_bucket=identity.formal_bucket,
                        formal_key=identity.formal_key,
                        owner_kind=owner_kind,
                        owner_token=token,
                        owner_generation=generation,
                        state='held',
                        acquired_at=now,
                        lease_expires_at=self._clock_plus(lease_seconds),
                    )
                    self.session.add(fence)
                    created.append(fence)
                self.session.flush()
                return BindingFenceLease(
                    owner_token=token,
                    owner_generation=generation,
                    fence_ids=tuple(fence.id for fence in created),
                    identities=canonical,
                )
        except Exception:
            self.session.rollback()
            raise

    def acquire_prewrite(self, identities, *, owner_kind: str, control_session_factory, lease_seconds=300):
        """Acquire with an independent short-lived control session."""
        if control_session_factory is None:
            raise ValueError('control session factory is required')
        control = control_session_factory()
        try:
            return ObjectBindingFenceService(control, purge_fence_service=self._purge_fence_service).acquire(
                identities, owner_kind=owner_kind, lease_seconds=lease_seconds,
            )
        finally:
            control.close()

    def renew_prewrite(self, lease, *, control_session_factory, lease_seconds=300):
        """Renew through a fresh control session; never touches caller scope."""
        if control_session_factory is None:
            raise ValueError('control session factory is required')
        if lease is None:
            return False
        control = control_session_factory()
        try:
            return ObjectBindingFenceService(control, purge_fence_service=self._purge_fence_service).renew(
                lease, lease_seconds=lease_seconds,
            )
        finally:
            control.close()

    def renew(self, lease: BindingFenceLease, *, lease_seconds: int) -> bool:
        from models import ObjectBindingFence

        try:
            with self.session.begin():
                self._lock_all(lease.identities)
                now = self._clock()
                rows = self.session.execute(
                    select(ObjectBindingFence)
                    .where(ObjectBindingFence.id.in_(lease.fence_ids))
                    .with_for_update()
                ).scalars().all()
                if not self._owns_complete_set(rows, lease, now):
                    raise BindingFenceHeld('绑定租约已失效')
                for row in rows:
                    row.lease_expires_at = self._clock_plus(lease_seconds)
            return True
        except BindingFenceHeld:
            self.session.rollback()
            return False

    def release(self, lease: BindingFenceLease, *, reason: str) -> bool:
        from models import ObjectBindingFence

        try:
            with self.session.begin():
                self._lock_all(lease.identities)
                now = self._clock()
                rows = self.session.execute(
                    select(ObjectBindingFence).where(
                        ObjectBindingFence.id.in_(lease.fence_ids)
                    ).with_for_update()
                ).scalars().all()
                if not self._owns_complete_set(rows, lease, now):
                    raise BindingFenceHeld('绑定租约已失效')
                for row in rows:
                    row.state = 'released'
                    row.released_at = now
                    row.release_reason = reason
            return True
        except BindingFenceHeld:
            self.session.rollback()
            return False

    def final_bind(self, lease: BindingFenceLease, *, bind, release: bool = True) -> bool:
        """Bind only while the complete live lease set remains owned."""
        from models import ObjectBindingFence

        try:
            with self.session.begin():
                self._lock_all(lease.identities)
                now = self._clock()
                rows = self.session.execute(
                    select(ObjectBindingFence).where(
                        ObjectBindingFence.id.in_(lease.fence_ids)
                    ).with_for_update()
                ).scalars().all()
                if not self._owns_complete_set(rows, lease, now):
                    raise BindingFenceHeld('绑定租约已失效')
                outcome = bind()
                if outcome is False:
                    raise BindingFenceHeld('绑定操作已失效')
                if release:
                    for row in rows:
                        row.state = 'released'
                        row.released_at = now
                        row.release_reason = 'completed'
            return True
        except BindingFenceHeld:
            self.session.rollback()
            return False

    def finalize_in_transaction(self, lease, caller_session, bind) -> bool:
        """Caller-owned variant: never opens or commits the caller transaction."""
        if lease is None:
            return False
        from models import ObjectBindingFence
        now = caller_session.execute(text('SELECT clock_timestamp()::timestamp')).scalar_one()
        rows = caller_session.execute(
            select(ObjectBindingFence).where(
                ObjectBindingFence.id.in_(lease.fence_ids)
            ).with_for_update()
        ).scalars().all()
        if (
            len(rows) != len(lease.fence_ids)
            or {row.id for row in rows} != set(lease.fence_ids)
            or any(
                row.owner_token != lease.owner_token
                or row.owner_generation != lease.owner_generation
                or row.state != 'held'
                or row.lease_expires_at <= now
                for row in rows
            )
        ):
            return False
        if not bind():
            return False
        for row in rows:
            row.state = 'released'
            row.released_at = now
            row.release_reason = 'completed'
        return True

    def abort_after_rollback(self, lease, *, control_session_factory=None) -> bool:
        """Best-effort explicit release after a caller-owned outer rollback."""
        if lease is None:
            return False
        if control_session_factory is not None:
            control = control_session_factory()
            try:
                return ObjectBindingFenceService(
                    control, purge_fence_service=self._purge_fence_service,
                ).release(lease, reason='failed')
            finally:
                control.close()
        return self.release(lease, reason='failed')

    def assert_purge_available(self, identities) -> None:
        """Called inside purge's complete-set lock transaction."""
        from models import ObjectBindingFence

        canonical = self._canonical(identities)
        self._lock_all(canonical)
        now = self._clock()
        for identity in canonical:
            held = self.session.execute(
                select(ObjectBindingFence)
                .where(
                    ObjectBindingFence.formal_bucket == identity.formal_bucket,
                    ObjectBindingFence.formal_key == identity.formal_key,
                    ObjectBindingFence.state == 'held',
                ).with_for_update()
            ).scalar_one_or_none()
            if held is None:
                continue
            if held.lease_expires_at > now:
                raise BindingFenceHeld('正式对象存在有效绑定租约')
            held.state = 'released'
            held.released_at = now
            held.release_reason = 'lease_expired'

    def _lock_all(self, identities):
        for identity in identities:
            self.session.execute(
                text('SELECT pg_advisory_xact_lock(hashtextextended(:value, 0))'),
                {'value': f'{identity.formal_bucket}:{identity.formal_key}'},
            )

    def _clock(self):
        return self.session.execute(
            text('SELECT clock_timestamp()::timestamp')
        ).scalar_one()

    def _clock_plus(self, seconds):
        return self.session.execute(
            text("SELECT (clock_timestamp() + (:seconds * interval '1 second'))::timestamp"),
            {'seconds': seconds},
        ).scalar_one()

    @staticmethod
    def _owns_complete_set(rows, lease, now):
        return (
            len(rows) == len(lease.fence_ids)
            and {row.id for row in rows} == set(lease.fence_ids)
            and all(
                row.owner_token == lease.owner_token
                and row.state == 'held'
                and row.lease_expires_at > now
                for row in rows
            )
        )
