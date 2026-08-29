"""Archived image-asset listing and atomic restore operations."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy import func, or_, select, update

from services.asset_activity import activity_state
from services.asset_display_name import management_asset_dict


@dataclass(frozen=True)
class ArchivedAssetPage:
    assets: list[dict]
    total: int
    archived_total: int
    page: int
    per_page: int


@dataclass(frozen=True)
class RestoreItemResult:
    asset_id: str
    status: str
    version: int | None = None
    error_code: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        result = {'asset_id': self.asset_id, 'status': self.status}
        if self.version is not None:
            result['version'] = self.version
        if self.error_code is not None:
            result['error_code'] = self.error_code
        if self.error is not None:
            result['error'] = self.error
        return result


@dataclass(frozen=True)
class RestoreBatchResult:
    batch_id: str
    status: str
    restored_count: int
    already_active_count: int
    items: list[RestoreItemResult]

    def to_dict(self) -> dict:
        return {
            'batch_id': self.batch_id,
            'status': self.status,
            'restored_count': self.restored_count,
            'already_active_count': self.already_active_count,
            'items': [item.to_dict() for item in self.items],
        }


class RestoreRequestValidationError(ValueError):
    """The restore request shape is invalid and has no stable audit target."""

    error_code = 'INVALID_IMAGE_ASSET_RESTORE_BATCH'


class RestoreBlockedByPurgeBatch(Exception):
    """Archived assets held by a non-cancelled purge batch cannot be restored."""

    error_code = 'PURGE_ASSET_RESTORE_BLOCKED'

    def __init__(self, batch_id):
        super().__init__('图片属于未取消的永久清除批次，无法恢复')
        self.batch_id = batch_id


_RESTORE_ERRORS = {
    'IMAGE_ASSET_DUPLICATE_TARGET': '图片资产 ID 重复',
    'IMAGE_ASSET_NOT_FOUND': '图片资产不存在',
    'IMAGE_ASSET_INVALID_STATUS': '图片资产当前状态不支持恢复',
    'IMAGE_ASSET_ALREADY_ASSIGNED': '已归款的归档图片不能从未归款回收站恢复',
}


def _literal_ilike_pattern(value: str) -> str:
    escaped = value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{escaped}%'


def list_archived_image_assets(
    session,
    *,
    page: int,
    per_page: int,
    search: str,
) -> ArchivedAssetPage:
    """Return one stable page of archived assets without ending the transaction."""
    from models import ImageAsset

    archived_filter = ImageAsset.status == 'archived'
    archived_total = session.execute(
        select(func.count()).select_from(ImageAsset).where(archived_filter)
    ).scalar_one()

    search = search.strip()
    filters = [archived_filter]
    if search:
        pattern = _literal_ilike_pattern(search)
        filters.append(or_(
            ImageAsset.display_name.ilike(pattern, escape='\\'),
            ImageAsset.source_relative_path.ilike(pattern, escape='\\'),
        ))
        total = session.execute(
            select(func.count()).select_from(ImageAsset).where(*filters)
        ).scalar_one()
    else:
        total = archived_total

    assets = session.execute(
        select(ImageAsset)
        .where(*filters)
        .order_by(
            ImageAsset.archived_at.desc().nullslast(),
            ImageAsset.id.desc(),
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).scalars().all()
    return ArchivedAssetPage(
        assets=[management_asset_dict(asset) for asset in assets],
        total=total,
        archived_total=archived_total,
        page=page,
        per_page=per_page,
    )


def _parse_asset_ids(asset_ids: object) -> list[uuid.UUID]:
    if not isinstance(asset_ids, list) or not 1 <= len(asset_ids) <= 100:
        raise RestoreRequestValidationError(
            '图片恢复批次必须包含 1 至 100 个图片资产 ID'
        )
    if any(not isinstance(value, str) for value in asset_ids):
        raise RestoreRequestValidationError('图片恢复批次包含无效的图片资产 ID')
    try:
        return [uuid.UUID(value) for value in asset_ids]
    except (ValueError, AttributeError):
        raise RestoreRequestValidationError(
            '图片恢复批次包含无效的图片资产 ID'
        ) from None


def _record_activity(
    session,
    *,
    batch_id: str,
    request_id: str,
    result: RestoreBatchResult,
    states: dict[uuid.UUID, tuple[dict | None, dict | None]],
    request_count: int,
) -> None:
    from models import AssetActivityRecord

    records = [AssetActivityRecord(
        event_type='asset.restore.batch',
        target_type='image_asset_batch',
        target_id=batch_id,
        batch_id=batch_id,
        request_id=request_id[:64],
        source='api',
        before_state={'requested_count': request_count},
        after_state={
            'restored_count': result.restored_count,
            'already_active_count': result.already_active_count,
            'unchanged_count': sum(
                item.status == 'unchanged' for item in result.items
            ),
            'rejected_count': sum(
                item.status == 'rejected' for item in result.items
            ),
        },
        result=result.status,
    )]
    for item in result.items:
        before_state, after_state = states[uuid.UUID(item.asset_id)]
        records.append(AssetActivityRecord(
            event_type='asset.restore',
            target_type='image_asset',
            target_id=item.asset_id,
            batch_id=batch_id,
            request_id=request_id[:64],
            source='api',
            before_state=before_state,
            after_state=after_state,
            result='noop' if item.status == 'already_active' else item.status,
            error_code=item.error_code,
        ))
    session.add_all(records)


def restore_image_assets(
    session,
    asset_ids: object,
    *,
    request_id: str,
    actor_id: str | None = None,
) -> RestoreBatchResult:
    """Restore a bounded image-asset batch atomically with activity records."""
    from models import AssetActivityRecord, ImageAsset, PurgeBatch, PurgeBatchItem

    requested_ids = _parse_asset_ids(asset_ids)
    unique_requested_ids = list(dict.fromkeys(requested_ids))
    duplicate_ids = {
        asset_id for asset_id in requested_ids
        if requested_ids.count(asset_id) > 1
    }
    lock_ids = sorted(unique_requested_ids, key=str)
    try:
        locked = session.execute(
            select(ImageAsset)
            .where(ImageAsset.id.in_(lock_ids))
            .order_by(ImageAsset.id)
            .with_for_update()
        ).scalars().all()
    except Exception:
        session.rollback()
        raise

    if locked:
        blocking = session.execute(
            select(PurgeBatchItem, PurgeBatch)
            .join(PurgeBatch, PurgeBatch.id == PurgeBatchItem.batch_id)
            .where(PurgeBatchItem.target_asset_id.in_([asset.id for asset in locked]))
            .where(PurgeBatch.status != 'cancelled')
            .order_by(PurgeBatchItem.target_asset_id, PurgeBatch.id)
        ).first()
        if blocking:
            batch = blocking[1]
            session.add(AssetActivityRecord(
                event_type='asset.restore.rejected',
                target_type='image_asset',
                target_id=str(blocking[0].target_asset_id),
                batch_id=str(batch.id),
                request_id=request_id[:64],
                source='api',
                actor_id=actor_id,
                result='rejected',
                error_code='PURGE_ASSET_RESTORE_BLOCKED',
                after_state={'batch_id': str(batch.id), 'status': batch.status},
            ))
            session.commit()
            raise RestoreBlockedByPurgeBatch(batch.id)

    by_id = {asset.id: asset for asset in locked}
    errors = {}
    for asset_id in unique_requested_ids:
        asset = by_id.get(asset_id)
        if asset_id in duplicate_ids:
            errors[asset_id] = 'IMAGE_ASSET_DUPLICATE_TARGET'
        elif asset is None:
            errors[asset_id] = 'IMAGE_ASSET_NOT_FOUND'
        elif asset.status not in {'active', 'archived'}:
            errors[asset_id] = 'IMAGE_ASSET_INVALID_STATUS'
        elif asset.status == 'archived' and asset.model_number is not None:
            errors[asset_id] = 'IMAGE_ASSET_ALREADY_ASSIGNED'

    if errors:
        items = []
        states = {}
        for asset_id in unique_requested_ids:
            asset = by_id.get(asset_id)
            state = activity_state(asset) if asset is not None else None
            error_code = errors.get(asset_id)
            items.append(RestoreItemResult(
                asset_id=str(asset_id),
                status='rejected' if error_code else 'unchanged',
                version=asset.version if asset is not None else None,
                error_code=error_code,
                error=_RESTORE_ERRORS.get(error_code),
            ))
            states[asset_id] = (state, state)
        result = RestoreBatchResult(
            batch_id=str(uuid.uuid4()),
            status='rejected',
            restored_count=0,
            already_active_count=0,
            items=items,
        )
        try:
            _record_activity(
                session,
                batch_id=result.batch_id,
                request_id=request_id,
                result=result,
                states=states,
                request_count=len(requested_ids),
            )
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise

    eligible_ids = [
        asset_id for asset_id in unique_requested_ids
        if by_id[asset_id].status == 'archived'
        and by_id[asset_id].model_number is None
    ]
    try:
        updated_rows = []
        if eligible_ids:
            updated_rows = session.execute(
                update(ImageAsset)
                .where(
                    ImageAsset.id.in_(eligible_ids),
                    ImageAsset.status == 'archived',
                    ImageAsset.model_number.is_(None),
                )
                .values(
                    status='active',
                    archived_at=None,
                    updated_at=func.now(),
                    version=ImageAsset.version + 1,
                )
                .returning(
                    ImageAsset.id,
                    ImageAsset.model_number,
                    ImageAsset.display_name,
                    ImageAsset.version,
                    ImageAsset.status,
                )
                .execution_options(synchronize_session=False)
            ).mappings().all()
        updated_by_id = {row['id']: row for row in updated_rows}
        if len(updated_by_id) != len(eligible_ids):
            raise RuntimeError('restore update count mismatch')

        items = []
        states = {}
        for asset_id in unique_requested_ids:
            asset = by_id[asset_id]
            before_state = activity_state(asset)
            if asset_id in updated_by_id:
                updated = updated_by_id[asset_id]
                after_state = activity_state(updated)
                items.append(RestoreItemResult(
                    str(asset_id), 'restored', updated['version']
                ))
            else:
                after_state = before_state
                items.append(RestoreItemResult(
                    str(asset_id), 'already_active', asset.version
                ))
            states[asset_id] = (before_state, after_state)

        result = RestoreBatchResult(
            batch_id=str(uuid.uuid4()),
            status='succeeded',
            restored_count=len(eligible_ids),
            already_active_count=len(unique_requested_ids) - len(eligible_ids),
            items=items,
        )
        _record_activity(
            session,
            batch_id=result.batch_id,
            request_id=request_id,
            result=result,
            states=states,
            request_count=len(requested_ids),
        )
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
