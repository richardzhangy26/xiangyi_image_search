"""导入项暂存对象的引用安全清理。

规则（Issue #22）：
- 只处理已到期且未清理过的终态项（cancelled/failed/abandoned，asset_id 为空）。
- 原图仅在没有任何正式资产（active+archived）或其他未清理导入项引用时删除。
- 共享预览同规则；回收站（archived）资产的引用永远保护对象。
- 每项一个检查点（objects_purged_at），失败不置标记，可重启幂等续跑。
- 对象已不存在视为成功（'already_gone'）。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select

from models import AssetActivityRecord, ImageAsset, ImageImportItem
from services import import_retention
from services.object_storage import ObjectStorageError


logger = logging.getLogger(__name__)

DEFAULT_CLEANUP_BATCH = 50


def count_object_references(
    session,
    *,
    key: str,
    asset_column,
    item_column,
    exclude_item_id,
) -> int:
    """统计对象的活引用数：全部正式资产 + 未清理过的其他导入项。"""
    asset_refs = session.execute(
        select(func.count(ImageAsset.id)).where(asset_column == key)
    ).scalar_one()
    item_refs = session.execute(
        select(func.count(ImageImportItem.id)).where(
            item_column == key,
            ImageImportItem.id != exclude_item_id,
            ImageImportItem.objects_purged_at.is_(None),
        )
    ).scalar_one()
    return int(asset_refs) + int(item_refs)


def cleanup_one_item(session, item_id, *, storage, now=None) -> bool:
    """在独立短事务内清理一个导入项的暂存对象；返回是否完成清理。"""
    now = now or datetime.now()
    try:
        item = session.execute(
            select(ImageImportItem)
            .where(ImageImportItem.id == item_id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            item is None
            or item.asset_id is not None
            or not import_retention.is_purge_eligible(
                status=item.status,
                purge_eligible_at=item.purge_eligible_at,
                objects_purged_at=item.objects_purged_at,
                now=now,
            )
        ):
            session.rollback()
            return False

        outcomes = {}
        for label, key, asset_column, item_column in (
            (
                'original',
                item.oss_path,
                ImageAsset.oss_path,
                ImageImportItem.oss_path,
            ),
            (
                'preview',
                item.preview_oss_path,
                ImageAsset.preview_oss_path,
                ImageImportItem.preview_oss_path,
            ),
        ):
            references = count_object_references(
                session,
                key=key,
                asset_column=asset_column,
                item_column=item_column,
                exclude_item_id=item.id,
            )
            if references > 0:
                outcomes[label] = 'kept_referenced'
            else:
                outcomes[label] = storage.delete_object(key)

        expired_naturally = item.status != 'abandoned'
        item.objects_purged_at = now
        item.updated_at = now
        if expired_naturally:
            session.add(AssetActivityRecord(
                event_type='image_import.expired',
                target_type='image_import_item',
                target_id=str(item.id),
                task_id=str(item.id),
                request_id=item.request_id[:64],
                source='cleanup',
                before_state={'status': item.status},
                after_state={'purge_eligible_at': item.purge_eligible_at.isoformat()},
                result='expired',
            ))
        session.add(AssetActivityRecord(
            event_type='image_import.objects_purged',
            target_type='image_import_item',
            target_id=str(item.id),
            task_id=str(item.id),
            request_id=item.request_id[:64],
            source='cleanup',
            before_state={'status': item.status},
            after_state={'objects': outcomes},
            result='purged',
        ))
        session.commit()
        logger.info(
            'import_cleanup.purged item_id=%s status=%s objects=%s',
            item.id,
            item.status,
            outcomes,
        )
        return True
    except ObjectStorageError as exc:
        session.rollback()
        logger.error(
            'import_cleanup.object_delete_failed item_id=%s error_type=%s',
            item_id,
            type(exc).__name__,
        )
        return False
    except Exception:
        session.rollback()
        raise


def cleanup_expired_imports(
    session,
    *,
    storage,
    now=None,
    limit: int = DEFAULT_CLEANUP_BATCH,
) -> int:
    """扫描到期未清理项并逐项清理；每项独立事务，失败跳过、可重启续跑。"""
    now = now or datetime.now()
    candidates = session.execute(
        select(ImageImportItem.id)
        .where(
            ImageImportItem.status.in_(
                import_retention.PURGE_ELIGIBLE_STATUSES
            ),
            ImageImportItem.purge_eligible_at.is_not(None),
            ImageImportItem.purge_eligible_at <= now,
            ImageImportItem.objects_purged_at.is_(None),
            ImageImportItem.asset_id.is_(None),
        )
        .order_by(ImageImportItem.purge_eligible_at, ImageImportItem.id)
        .limit(limit)
    ).scalars().all()

    processed = 0
    for item_id in candidates:
        if cleanup_one_item(session, item_id, storage=storage, now=now):
            processed += 1
    return processed
