"""永久清除对象备份使用的完整 PostgreSQL 引用快照。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select, text

from services.purge_object_backup import (
    CompleteReferenceSnapshot,
    ObjectReference,
    PurgeAssetSnapshot,
    PurgeObjectReferenceError,
    ReferenceSourceSlice,
    REFERENCE_CATALOG_VERSION,
)


class PostgresReferenceSnapshotReader:
    """在一个只读可重复读边界内枚举所有仍有效的对象引用。"""

    def __init__(self, session, *, clock=lambda: datetime.now(timezone.utc), max_age_seconds=60):
        self.session = session
        self.clock = clock
        self.max_age_seconds = max_age_seconds

    def capture_for_purge(self, asset_ids: tuple[str, ...]) -> CompleteReferenceSnapshot:
        from models import ImageAsset, ImageImportItem

        try:
            selected = tuple(sorted(str(value) for value in asset_ids))
            if not selected or len(selected) != len(set(selected)):
                raise ValueError
        except (TypeError, ValueError):
            raise PurgeObjectReferenceError('实时引用快照目标无效') from None

        with self.session.begin():
            if self.session.get_bind().dialect.name == 'postgresql':
                self.session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            assets = self.session.execute(select(ImageAsset).order_by(ImageAsset.id)).scalars().all()
            imports = self.session.execute(
                select(ImageImportItem)
                .where(ImageImportItem.objects_purged_at.is_(None))
                .order_by(ImageImportItem.id)
            ).scalars().all()

            by_id = {str(asset.id): asset for asset in assets}
            if any(asset_id not in by_id or by_id[asset_id].status != 'archived' for asset_id in selected):
                raise PurgeObjectReferenceError('永久清除引用快照只接受存在的归档图片')
            targets = tuple(
                PurgeAssetSnapshot(
                    asset_id=asset_id,
                    status='archived',
                    original_key=by_id[asset_id].oss_path,
                    preview_key=by_id[asset_id].preview_oss_path,
                    original_size=by_id[asset_id].source_size,
                    original_sha256=by_id[asset_id].content_hash,
                    normalization_version=by_id[asset_id].normalization_version,
                )
                for asset_id in selected
            )
            asset_refs = [
                ObjectReference('image_assets', str(asset.id), asset.status, kind, key)
                for asset in assets
                for kind, key in (('source_image', asset.oss_path), ('search_preview', asset.preview_oss_path))
            ]
            import_refs = [
                ObjectReference('image_import_items', str(item.id), 'unfinished', 'search_preview', item.preview_oss_path)
                for item in imports
            ]
            references = tuple(sorted(
                asset_refs + import_refs,
                key=lambda item: (item.source, item.owner_id, item.kind, item.formal_key),
            ))
            token_payload = [
                (item.source, item.owner_id, item.owner_state, item.kind, item.formal_key)
                for item in references
            ]
            token = hashlib.sha256(json.dumps(token_payload, separators=(',', ':')).encode()).hexdigest()
            slices = (
                ReferenceSourceSlice('image_assets', token, 'complete', False, len(asset_refs)),
                ReferenceSourceSlice('image_import_items', token, 'complete', False, len(import_refs)),
            )
            return CompleteReferenceSnapshot(
                catalog_version=REFERENCE_CATALOG_VERSION,
                consistency_token=token,
                captured_at=self.clock(),
                targets=targets,
                source_slices=slices,
                references=references,
            )
