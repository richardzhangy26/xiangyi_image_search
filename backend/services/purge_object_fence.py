"""Shared formal-object fence lookup for Issue #27 binding paths."""

from dataclasses import dataclass

from sqlalchemy import select, text


class ObjectBindingBlocked(RuntimeError):
    error_code = 'PURGE_OBJECT_FENCE_HELD'


@dataclass(frozen=True, order=True)
class ObjectIdentity:
    formal_bucket: str
    formal_key: str

    def __post_init__(self):
        if not self.formal_bucket or not self.formal_key:
            raise ValueError('正式对象身份不能为空')


@dataclass(frozen=True)
class CurrentReferenceDecision:
    asset_reference_count: int
    import_reference_count: int

    @property
    def has_other_references(self) -> bool:
        return bool(self.asset_reference_count or self.import_reference_count)


class PurgeObjectFenceService:
    """Reads held fence epochs under row lock before a formal binding is written."""

    def __init__(self, session, *, binding_fence_service=None):
        self.session = session
        self._binding_fence_service = binding_fence_service

    @staticmethod
    def canonical_identities(identities):
        return tuple(sorted(set(identities)))

    def assert_bindable(self, identities):
        from models import PurgeObjectFence

        for identity in self.canonical_identities(identities):
            held = self.session.execute(
                select(PurgeObjectFence.id)
                .where(
                    PurgeObjectFence.formal_bucket == identity.formal_bucket,
                    PurgeObjectFence.formal_key == identity.formal_key,
                    PurgeObjectFence.state == 'held',
                )
                .with_for_update()
            ).first()
            if held is not None:
                raise ObjectBindingBlocked('正式对象正处于永久清除围栏中')

    def acquire_for_deletion(
        self,
        *,
        batch_id,
        target_asset_id,
        identity: ObjectIdentity,
        kind: str,
        audit_retain_until,
    ):
        """Create one held epoch after taking the canonical transaction lock."""
        from models import PurgeObjectFence

        if self._binding_fence_service is not None:
            self._binding_fence_service.assert_purge_available((identity,))
        self._advisory_lock(identity)
        existing = self.session.execute(
            select(PurgeObjectFence)
            .where(
                PurgeObjectFence.formal_bucket == identity.formal_bucket,
                PurgeObjectFence.formal_key == identity.formal_key,
                PurgeObjectFence.state == 'held',
            )
            .with_for_update()
        ).scalar_one_or_none()
        if existing is not None:
            if (
                existing.batch_id == batch_id
                and existing.target_asset_id == target_asset_id
            ):
                return existing
            raise ObjectBindingBlocked('正式对象已被另一个清除项围栏占用')
        fence = PurgeObjectFence(
            formal_bucket=identity.formal_bucket,
            formal_key=identity.formal_key,
            kind=kind,
            batch_id=batch_id,
            target_asset_id=target_asset_id,
            audit_retain_until=audit_retain_until,
        )
        self.session.add(fence)
        self.session.flush()
        return fence

    def release(self, fence_id, *, released_at):
        from models import PurgeObjectFence

        fence = self.session.execute(
            select(PurgeObjectFence)
            .where(PurgeObjectFence.id == fence_id)
            .with_for_update()
        ).scalar_one()
        fence.state = 'released'
        fence.released_at = released_at
        return fence

    def current_references(self, *, target_asset_id, identity: ObjectIdentity, kind: str):
        """Read current object references while the caller owns its fence lock."""
        from models import ImageAsset, ImageImportItem

        if kind == 'source_image':
            asset_column = ImageAsset.oss_path
            import_column = ImageImportItem.oss_path
        elif kind == 'search_preview':
            asset_column = ImageAsset.preview_oss_path
            import_column = ImageImportItem.preview_oss_path
        else:
            raise ValueError('未知正式对象类型')

        assets = self.session.execute(
            select(ImageAsset.id)
            .where(asset_column == identity.formal_key, ImageAsset.id != target_asset_id)
            .with_for_update()
        ).scalars().all()
        imports = self.session.execute(
            select(ImageImportItem.id, ImageImportItem.status, ImageImportItem.asset_id)
            .where(
                import_column == identity.formal_key,
                ImageImportItem.objects_purged_at.is_(None),
            )
            .with_for_update()
        ).all()
        protected_imports = [
            item_id for item_id, status, asset_id in imports
            if not (status == 'completed' and asset_id == target_asset_id)
        ]
        return CurrentReferenceDecision(
            asset_reference_count=len(assets),
            import_reference_count=len(protected_imports),
        )

    def _advisory_lock(self, identity: ObjectIdentity):
        if self.session.get_bind().dialect.name != 'postgresql':
            return
        material = f'{identity.formal_bucket}:{identity.formal_key}'
        self.session.execute(
            text('SELECT pg_advisory_xact_lock(hashtextextended(:material, 0))'),
            {'material': material},
        )
