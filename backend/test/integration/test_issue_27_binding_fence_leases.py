"""Issue #27 binding-fence leases run against real PostgreSQL sessions."""

import uuid

import pytest
from sqlalchemy import text


def _identities():
    from services.purge_object_fence import ObjectIdentity

    return (
        ObjectIdentity('formal-test-bucket', 'original/a.png'),
        ObjectIdentity('formal-test-bucket', 'preview/a.jpg'),
    )


def test_live_owner_blocks_complete_set_and_expired_takeover_invalidates_old_token(pg_session_factory):
    from services.object_binding_fence import (
        BindingFenceHeld,
        ObjectBindingFenceService,
    )

    first_session = pg_session_factory()
    second_session = pg_session_factory()
    try:
        first = ObjectBindingFenceService(first_session).acquire(
            _identities(), owner_kind='asset_ingest', lease_seconds=60,
        )
        with pytest.raises(BindingFenceHeld):
            ObjectBindingFenceService(second_session).acquire(
                _identities(), owner_kind='asset_ingest', lease_seconds=60,
            )

        first_session.execute(text(
            "UPDATE object_binding_fences SET acquired_at = clock_timestamp() - interval '2 seconds', "
            "lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE owner_token = :token"
        ), {'token': str(first.owner_token)})
        first_session.commit()

        second = ObjectBindingFenceService(second_session).acquire(
            _identities(), owner_kind='asset_ingest', lease_seconds=60,
        )
        assert second.owner_token != first.owner_token
        assert second.owner_generation == first.owner_generation + 1
        assert ObjectBindingFenceService(first_session).renew(first, lease_seconds=60) is False
    finally:
        first_session.close()
        second_session.close()


def test_multikey_conflict_leaves_no_partial_owner_epochs(pg_session_factory):
    from services.object_binding_fence import (
        BindingFenceHeld,
        ObjectBindingFenceService,
    )
    from models import ObjectBindingFence

    one, two = pg_session_factory(), pg_session_factory()
    identities = _identities()
    try:
        ObjectBindingFenceService(one).acquire(
            (identities[1],), owner_kind='asset_ingest', lease_seconds=60,
        )
        with pytest.raises(BindingFenceHeld):
            ObjectBindingFenceService(two).acquire(
                identities, owner_kind='asset_ingest', lease_seconds=60,
            )
        assert two.query(ObjectBindingFence).filter_by(
            formal_key='original/a.png', state='held'
        ).count() == 0
    finally:
        one.close()
        two.close()


def test_final_bind_releases_complete_owner_set_once(pg_session_factory):
    from services.object_binding_fence import ObjectBindingFenceService
    from models import ObjectBindingFence

    session = pg_session_factory()
    try:
        service = ObjectBindingFenceService(session)
        lease = service.acquire(
            _identities(), owner_kind='asset_ingest', lease_seconds=60,
        )
        calls = []
        assert service.final_bind(lease, bind=lambda: calls.append('bound')) is True
        assert calls == ['bound']
        assert session.query(ObjectBindingFence).filter_by(
            owner_token=lease.owner_token, state='held'
        ).count() == 0
    finally:
        session.close()


def test_live_binding_lease_blocks_purge_fence_acquisition(pg_session_factory):
    from services.object_binding_fence import (
        BindingFenceHeld,
        ObjectBindingFenceService,
    )
    from services.purge_object_fence import PurgeObjectFenceService

    binding_session, purge_session = pg_session_factory(), pg_session_factory()
    try:
        ObjectBindingFenceService(binding_session).acquire(
            _identities(), owner_kind='asset_ingest', lease_seconds=60,
        )
        with pytest.raises(BindingFenceHeld):
            with purge_session.begin():
                PurgeObjectFenceService(
                    purge_session,
                    binding_fence_service=ObjectBindingFenceService(purge_session),
                ).acquire_for_deletion(
                    batch_id=uuid.uuid4(), target_asset_id=uuid.uuid4(),
                    identity=_identities()[0], kind='source_image',
                    audit_retain_until=ObjectBindingFenceService(purge_session)._clock_plus(3600),
                )
    finally:
        binding_session.close()
        purge_session.close()


def test_live_purge_fence_blocks_binding_acquisition_before_oss(pg_session_factory):
    from services.object_binding_fence import (
        BindingFenceHeld,
        ObjectBindingFenceService,
    )
    from services.purge_object_fence import PurgeObjectFenceService

    purge_session, binding_session = pg_session_factory(), pg_session_factory()
    try:
        with purge_session.begin():
            PurgeObjectFenceService(purge_session).acquire_for_deletion(
                batch_id=uuid.uuid4(), target_asset_id=uuid.uuid4(),
                identity=_identities()[0], kind='source_image',
                audit_retain_until=ObjectBindingFenceService(purge_session)._clock_plus(3600),
            )
        with pytest.raises(BindingFenceHeld):
            ObjectBindingFenceService(
                binding_session,
                purge_fence_service=PurgeObjectFenceService(binding_session),
            ).acquire(
                (_identities()[0],), owner_kind='asset_ingest', lease_seconds=60,
            )
    finally:
        purge_session.close()
        binding_session.close()


def test_control_session_renew_prewrite_respects_live_expired_and_released_epochs(pg_session_factory):
    from sqlalchemy import text
    from services.object_binding_fence import ObjectBindingFenceService

    factory = pg_session_factory
    lease = ObjectBindingFenceService(factory()).acquire_prewrite(
        _identities(), owner_kind='asset_ingest', control_session_factory=factory, lease_seconds=60,
    )
    service = ObjectBindingFenceService(object())
    assert service.renew_prewrite(lease, control_session_factory=factory, lease_seconds=60) is True
    control = factory()
    try:
        control.execute(text("UPDATE object_binding_fences SET acquired_at = clock_timestamp() - interval '2 seconds', lease_expires_at = clock_timestamp() - interval '1 second' WHERE owner_token = :t"), {'t': str(lease.owner_token)})
        control.commit()
    finally:
        control.close()
    assert service.renew_prewrite(lease, control_session_factory=factory, lease_seconds=60) is False
    control = factory()
    try:
        control.execute(text("UPDATE object_binding_fences SET state = 'released', released_at = clock_timestamp(), release_reason = 'failed' WHERE owner_token = :t"), {'t': str(lease.owner_token)})
        control.commit()
    finally:
        control.close()
    assert service.renew_prewrite(lease, control_session_factory=factory, lease_seconds=60) is False
