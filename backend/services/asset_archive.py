"""Atomic lifecycle archive command for unassigned image assets."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy import func, select, update

from services.asset_activity import activity_state


@dataclass(frozen=True)
class ArchiveItemResult:
    asset_id: str
    status: str
    version: int | None = None
    error_code: str | None = None

    def to_dict(self) -> dict:
        result = {'asset_id': self.asset_id, 'status': self.status}
        if self.version is not None:
            result['version'] = self.version
        if self.error_code is not None:
            result['error_code'] = self.error_code
        return result


@dataclass(frozen=True)
class ArchiveBatchResult:
    batch_id: str
    status: str
    archived_count: int
    already_archived_count: int
    items: list[ArchiveItemResult]

    def to_dict(self) -> dict:
        return {
            'batch_id': self.batch_id,
            'status': self.status,
            'archived_count': self.archived_count,
            'already_archived_count': self.already_archived_count,
            'items': [item.to_dict() for item in self.items],
        }


class ArchiveRequestValidationError(ValueError):
    """The archive request shape is invalid and must not be audited."""

    error_code = 'INVALID_IMAGE_ASSET_ARCHIVE_BATCH'


def _parse_asset_ids(asset_ids: object) -> list[uuid.UUID]:
    if not isinstance(asset_ids, list) or not 1 <= len(asset_ids) <= 100:
        raise ArchiveRequestValidationError('图片归档批次必须包含 1 至 100 个图片资产 ID')
    if any(not isinstance(value, str) for value in asset_ids):
        raise ArchiveRequestValidationError('图片归档批次包含无效的图片资产 ID')
    try:
        parsed = [uuid.UUID(value) for value in asset_ids]
    except (ValueError, AttributeError):
        raise ArchiveRequestValidationError('图片归档批次包含无效的图片资产 ID') from None
    return parsed


def _record_activity(
    session, *, batch_id: str, request_id: str, result: ArchiveBatchResult,
    states: dict[uuid.UUID, tuple[dict | None, dict | None]], request_count: int,
) -> None:
    from models import AssetActivityRecord

    records = [AssetActivityRecord(
        event_type='asset.archive.batch',
        target_type='image_asset_batch',
        target_id=batch_id,
        batch_id=batch_id,
        request_id=request_id[:64],
        source='api',
        before_state={'requested_count': request_count},
        after_state={
            'archived_count': result.archived_count,
            'already_archived_count': result.already_archived_count,
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
            event_type='asset.archive',
            target_type='image_asset',
            target_id=item.asset_id,
            batch_id=batch_id,
            request_id=request_id[:64],
            source='api',
            before_state=before_state,
            after_state=after_state,
            result='noop' if item.status == 'already_archived' else item.status,
            error_code=item.error_code,
        ))
    session.add_all(records)


def archive_unassigned_image_assets(session, asset_ids: object, *, request_id: str) -> ArchiveBatchResult:
    """Archive only active unassigned assets, atomically with activity records."""
    from models import ImageAsset

    requested_ids = _parse_asset_ids(asset_ids)
    unique_requested_ids = list(dict.fromkeys(requested_ids))
    duplicate_ids = {
        asset_id for asset_id in requested_ids
        if requested_ids.count(asset_id) > 1
    }
    unique_ids = sorted(unique_requested_ids, key=str)
    try:
        locked = session.execute(
            select(ImageAsset)
            .where(ImageAsset.id.in_(unique_ids))
            .order_by(ImageAsset.id)
            .with_for_update()
        ).scalars().all()
    except Exception:
        session.rollback()
        raise
    by_id = {asset.id: asset for asset in locked}
    errors = {}
    for asset_id in unique_requested_ids:
        asset = by_id.get(asset_id)
        if asset_id in duplicate_ids:
            errors[asset_id] = 'IMAGE_ASSET_DUPLICATE_TARGET'
        elif asset is None:
            errors[asset_id] = 'IMAGE_ASSET_NOT_FOUND'
        elif asset.model_number is not None:
            errors[asset_id] = 'IMAGE_ASSET_ALREADY_ASSIGNED'
        elif asset.status not in {'active', 'archived'}:
            errors[asset_id] = 'IMAGE_ASSET_INVALID_STATUS'

    if errors:
        items = []
        states = {}
        for asset_id in unique_requested_ids:
            asset = by_id.get(asset_id)
            state = activity_state(asset) if asset is not None else None
            error_code = errors.get(asset_id)
            items.append(ArchiveItemResult(
                str(asset_id),
                'rejected' if error_code else 'unchanged',
                asset.version if asset is not None else None,
                error_code,
            ))
            states[asset_id] = (state, state)
        result = ArchiveBatchResult(
            batch_id=str(uuid.uuid4()), status='rejected',
            archived_count=0, already_archived_count=0, items=items,
        )
        try:
            _record_activity(
                session, batch_id=result.batch_id, request_id=request_id,
                result=result, states=states, request_count=len(requested_ids),
            )
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise

    eligible_ids = [
        asset_id for asset_id in requested_ids
        if by_id[asset_id].status == 'active'
        and by_id[asset_id].model_number is None
    ]
    try:
        updated_rows = session.execute(
            update(ImageAsset)
            .where(
                ImageAsset.id.in_(eligible_ids),
                ImageAsset.status == 'active',
                ImageAsset.model_number.is_(None),
            )
            .values(
                status='archived',
                archived_at=func.now(),
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
            raise RuntimeError('archive update count mismatch')

        items = []
        states = {}
        for asset_id in requested_ids:
            asset = by_id[asset_id]
            before_state = activity_state(asset)
            if asset_id in updated_by_id:
                updated = updated_by_id[asset_id]
                after_state = activity_state(updated)
                items.append(ArchiveItemResult(
                    str(asset_id), 'archived', updated['version']
                ))
            else:
                after_state = before_state
                items.append(ArchiveItemResult(
                    str(asset_id), 'already_archived', asset.version
                ))
            states[asset_id] = (before_state, after_state)

        result = ArchiveBatchResult(
            batch_id=str(uuid.uuid4()),
            status='succeeded',
            archived_count=len(eligible_ids),
            already_archived_count=len(requested_ids) - len(eligible_ids),
            items=items,
        )
        _record_activity(
            session, batch_id=result.batch_id, request_id=request_id,
            result=result, states=states, request_count=len(requested_ids),
        )
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
