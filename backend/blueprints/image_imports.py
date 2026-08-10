"""持久图片导入项的排队、查询、手工重试与取消 API。"""

from __future__ import annotations

import os
import uuid
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request
from flask_cors import cross_origin
from sqlalchemy import select
from werkzeug.exceptions import RequestEntityTooLarge

from models import (
    CANCELABLE_STATUSES,
    AssetActivityRecord,
    ImageImportItem,
    db,
)
from services import import_retention
from services.asset_ingest import (
    AssetIngestConflictError,
    AssetIngestError,
    ImageAssetIngestService,
)
from services.image_normalizer import ImageNormalizationError
from services.object_storage import ObjectStorageError, OssObjectStorage
from services.upload_source import prepare_multipart_source


image_imports_bp = Blueprint(
    'image_imports',
    __name__,
    url_prefix='/api/image-imports',
)

MAX_IMPORT_FILES = 20
MAX_CANCEL_BATCH = 100
IMPORT_SOURCE_PROVIDER = 'image-import-upload'
IMPORT_SOURCE_BUCKET = 'image-imports'
ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
UNRESOLVED_STATUSES = ('queued', 'embedding', 'failed', 'awaiting_retry')
PROCESSING_STATUSES = ('queued', 'embedding')


def _error(message, error_code, status_code):
    return jsonify({'error': message, 'error_code': error_code}), status_code


def _allowed_file(filename):
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS


def prepare_image_import_uploads(image_files):
    """为无型号图片生成跨刷新稳定、内容感知的来源身份。"""
    return prepare_multipart_source(
        image_files,
        source_bucket=IMPORT_SOURCE_BUCKET,
        is_allowed=_allowed_file,
        build_relative_path=lambda filename, content_hash, occurrence: (
            f'imports/{content_hash}/{occurrence:04d}/{filename}'
        ),
    )


def _get_ingest_service(source):
    storage = current_app.config.get('IMAGE_ASSET_STORAGE')
    if storage is None:
        storage = OssObjectStorage.from_env()
    return ImageAssetIngestService(
        source=source,
        storage=storage,
        embedding_client=current_app.config.get('IMAGE_INGEST_EMBEDDING'),
        normalizer=current_app.config.get('IMAGE_ASSET_NORMALIZER'),
        source_provider=IMPORT_SOURCE_PROVIDER,
    )


def _queue_result_dict(result):
    return {
        'item_id': result.item_id,
        'asset_id': result.asset_id,
        'source_relative_path': result.source_relative_path,
        'status': result.status,
        'recovery_action': result.recovery_action,
    }


@image_imports_bp.post('')
@cross_origin()
def create_image_imports():
    """校验并排队 1–20 张图片；响应前不执行 embedding。"""
    files = [
        item for item in request.files.getlist('images')
        if item and item.filename
    ]
    if not files:
        return _error('请至少选择一张图片', 'IMAGE_IMPORT_FILES_REQUIRED', 400)
    if len(files) > MAX_IMPORT_FILES:
        return _error(
            f'单次最多导入 {MAX_IMPORT_FILES} 张图片',
            'IMAGE_IMPORT_TOO_MANY_FILES',
            400,
        )
    if any(not _allowed_file(item.filename) for item in files):
        return _error('包含不支持的图片格式', 'IMAGE_IMPORT_FILE_TYPE', 400)

    source, relative_paths = prepare_image_import_uploads(files)
    request_id = uuid.uuid4().hex
    results = []
    try:
        service = _get_ingest_service(source)
        for relative_path in relative_paths:
            result = service.queue_one(
                relative_path,
                request_id=request_id,
                commit=False,
            )
            results.append(_queue_result_dict(result))
        db.session.commit()
    except AssetIngestConflictError:
        db.session.rollback()
        return _error(
            '来源冲突：同一来源身份已存在不同内容，未覆盖现有内容',
            'IMAGE_IMPORT_SOURCE_CONFLICT',
            409,
        )
    except ImageNormalizationError:
        db.session.rollback()
        return _error('图片已损坏或无法安全解码', 'INVALID_IMAGE', 400)
    except (ObjectStorageError, AssetIngestError) as exc:
        db.session.rollback()
        current_app.logger.error(
            '图片导入排队失败 stage=%s error_type=%s',
            getattr(exc, 'stage', 'storage'),
            type(exc).__name__,
        )
        return _error('图片导入排队失败', 'IMAGE_IMPORT_QUEUE_FAILED', 503)
    except RequestEntityTooLarge:
        db.session.rollback()
        return _error('上传图片过大', 'IMAGE_TOO_LARGE', 413)
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(
            '图片导入排队失败 error_type=%s',
            type(exc).__name__,
        )
        return _error('图片导入排队失败', 'IMAGE_IMPORT_QUEUE_FAILED', 500)

    queued_count = sum(item['status'] == 'queued' for item in results)
    status_code = 202 if queued_count else 200
    return jsonify({
        'items': results,
        'queued_count': queued_count,
    }), status_code


@image_imports_bp.get('')
@cross_origin()
def list_image_imports():
    try:
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        if page < 1 or not 1 <= per_page <= 100:
            raise ValueError
    except (TypeError, ValueError):
        return _error('分页参数无效', 'INVALID_IMAGE_IMPORT_LIST_PARAMS', 400)

    query = ImageImportItem.query.order_by(
        ImageImportItem.created_at.desc(),
        ImageImportItem.id.desc(),
    )
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    unresolved_count = ImageImportItem.query.filter(
        ImageImportItem.status.in_(UNRESOLVED_STATUSES)
    ).count()
    processing_count = ImageImportItem.query.filter(
        ImageImportItem.status.in_(PROCESSING_STATUSES)
    ).count()
    return jsonify({
        'items': [item.to_public_dict() for item in pagination.items],
        'total': pagination.total,
        'page': page,
        'per_page': per_page,
        'unresolved_count': unresolved_count,
        'processing_count': processing_count,
    })


@image_imports_bp.get('/<uuid:item_id>')
@cross_origin()
def get_image_import(item_id):
    item = db.session.get(ImageImportItem, item_id)
    if item is None:
        return _error('图片导入项不存在', 'IMAGE_IMPORT_NOT_FOUND', 404)
    return jsonify(item.to_public_dict())


@image_imports_bp.post('/<uuid:item_id>/retry')
@cross_origin()
def retry_image_import(item_id):
    """手工重试失败或等待重试的导入项。

    幂等、多 worker 安全：只把任务重新置为可领取，复用已验证的原图、预览与
    规范化结果；不直接调用 embedding，也不重置已消耗的自动尝试预算。
    """
    try:
        item = db.session.execute(
            select(ImageImportItem)
            .where(ImageImportItem.id == item_id)
            .with_for_update()
        ).scalar_one_or_none()
        if item is None:
            db.session.rollback()
            return _error('图片导入项不存在', 'IMAGE_IMPORT_NOT_FOUND', 404)

        if item.cancel_requested_at is not None:
            # 取消意图优先于重试：已请求取消的任务不得再手工重试。
            db.session.rollback()
            return _error(
                '该导入已请求取消，不能重试',
                'IMAGE_IMPORT_RETRY_CANCELLED',
                409,
            )

        if item.objects_purged_at is not None:
            # Issue #22：暂存对象已清理，无法再复用原图与预览。
            db.session.rollback()
            return _error(
                '该导入的暂存对象已清理，无法重试',
                'IMAGE_IMPORT_RETRY_PURGED',
                410,
            )

        if item.status == 'completed':
            db.session.rollback()
            return _error(
                '该导入已形成正式资产，无需重试',
                'IMAGE_IMPORT_RETRY_COMPLETED',
                409,
            )

        if item.status in ('queued', 'embedding'):
            # 已在处理或排队中：幂等返回当前状态，不打断在途领取。
            db.session.rollback()
            return jsonify(item.to_public_dict())

        if item.status == 'awaiting_retry':
            # 重复点击：只把下次领取时间提前到立即，不重复写转移记录。
            item.next_retry_at = datetime.now()
            db.session.commit()
            return jsonify(item.to_public_dict())

        before_status = item.status
        now = datetime.now()
        item.status = 'awaiting_retry'
        item.next_retry_at = now
        item.failed_at = None
        # Issue #22：手工重试重新计算保留窗口——清空到期时刻，
        # 若再次失败将按新的失败时刻重新计算 30 天。
        item.purge_eligible_at = None
        item.claim_token = None
        item.claimed_by = None
        item.claimed_at = None
        item.lease_expires_at = None
        item.updated_at = now
        db.session.add(AssetActivityRecord(
            event_type='image_import.manual_retry',
            target_type='image_import_item',
            target_id=str(item.id),
            task_id=str(item.id),
            request_id=uuid.uuid4().hex,
            source='api',
            before_state={'status': before_status},
            after_state={'status': 'awaiting_retry'},
            result='awaiting_retry',
        ))
        db.session.commit()
        return jsonify(item.to_public_dict())
    except Exception:
        db.session.rollback()
        current_app.logger.error(
            '图片导入手工重试失败 item_id=%s', item_id, exc_info=True
        )
        return _error('手工重试失败', 'IMAGE_IMPORT_RETRY_FAILED', 500)


def _apply_cancel_to_locked_item(item, requested_by, batch_id, now):
    """对已行锁定的导入项写入取消意图；返回逐项结果码。

    CANCELABLE_STATUSES 中的项直接落 cancelled 终态；embedding（在途）只持久化
    意图，由 worker 检查点落终态；completed 拒绝；已有意图按幂等成功处理。
    """
    if item.status == 'completed':
        return 'completed_rejected'
    if item.status == 'cancelled' or item.cancel_requested_at is not None:
        return 'already_cancelled'
    if item.status not in CANCELABLE_STATUSES:
        return 'not_cancelable'

    before_status = item.status
    item.cancel_requested_at = now
    item.cancel_requested_by = requested_by[:128]
    if item.status == 'embedding':
        event_type = 'image_import.cancel_requested'
        result_code = 'cancel_requested'
    else:
        item.status = 'cancelled'
        item.cancelled_at = now
        # Issue #22：取消项进入保留窗口（worker 侧转移同样写入）。
        item.purge_eligible_at = import_retention.cancel_purge_deadline(now)
        item.claim_token = None
        item.claimed_by = None
        item.claimed_at = None
        item.lease_expires_at = None
        item.next_retry_at = None
        event_type = 'image_import.cancelled'
        result_code = 'cancelled'
    db.session.add(AssetActivityRecord(
        event_type=event_type,
        target_type='image_import_item',
        target_id=str(item.id),
        task_id=str(item.id),
        request_id=uuid.uuid4().hex,
        source='api',
        batch_id=batch_id,
        before_state={'status': before_status},
        after_state={'status': item.status},
        result=result_code,
    ))
    return result_code


@image_imports_bp.post('/<uuid:item_id>/cancel')
@cross_origin()
def cancel_image_import(item_id):
    """单项取消；幂等。已完成项拒绝并引导回收站。"""
    try:
        item = db.session.execute(
            select(ImageImportItem)
            .where(ImageImportItem.id == item_id)
            .with_for_update()
        ).scalar_one_or_none()
        if item is None:
            db.session.rollback()
            return _error('图片导入项不存在', 'IMAGE_IMPORT_NOT_FOUND', 404)

        now = datetime.now()
        result = _apply_cancel_to_locked_item(
            item, request.remote_addr or 'api', uuid.uuid4().hex, now
        )
        if result == 'completed_rejected':
            db.session.rollback()
            return _error(
                '该导入已形成正式资产，不能取消；如需移除请使用回收站',
                'IMAGE_IMPORT_CANCEL_COMPLETED',
                409,
            )
        db.session.commit()
        payload = item.to_public_dict()
        payload['result'] = result
        return jsonify(payload)
    except Exception:
        db.session.rollback()
        current_app.logger.error(
            '图片导入取消失败 item_id=%s', item_id, exc_info=True
        )
        return _error('取消失败', 'IMAGE_IMPORT_CANCEL_FAILED', 500)


@image_imports_bp.post('/cancel')
@cross_origin()
def cancel_image_imports_batch():
    """批量取消（上限 100）；逐项结果，整体 200。"""
    body = request.get_json(silent=True) or {}
    item_ids = body.get('item_ids')
    if not isinstance(item_ids, list) or len(item_ids) == 0:
        return _error(
            '请至少提供一个导入项', 'IMAGE_IMPORT_CANCEL_REQUIRED', 400
        )
    if len(item_ids) > MAX_CANCEL_BATCH:
        return _error(
            f'单次最多取消 {MAX_CANCEL_BATCH} 个导入项',
            'IMAGE_IMPORT_CANCEL_TOO_MANY',
            400,
        )

    batch_id = uuid.uuid4().hex
    requested_by = request.remote_addr or 'api'
    now = datetime.now()
    results = []
    try:
        for raw_id in item_ids:
            try:
                parsed_id = uuid.UUID(str(raw_id))
            except (ValueError, AttributeError, TypeError):
                results.append({'item_id': str(raw_id), 'result': 'not_found'})
                continue
            item = db.session.execute(
                select(ImageImportItem)
                .where(ImageImportItem.id == parsed_id)
                .with_for_update()
            ).scalar_one_or_none()
            if item is None:
                results.append(
                    {'item_id': str(parsed_id), 'result': 'not_found'}
                )
                continue
            result = _apply_cancel_to_locked_item(
                item, requested_by, batch_id, now
            )
            results.append({'item_id': str(parsed_id), 'result': result})
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.error(
            '图片导入批量取消失败 batch_id=%s', batch_id, exc_info=True
        )
        return _error('批量取消失败', 'IMAGE_IMPORT_CANCEL_FAILED', 500)

    cancelled_count = sum(
        1 for item in results if item['result'] == 'cancelled'
    )
    return jsonify({
        'items': results,
        'cancelled_count': cancelled_count,
        'batch_id': batch_id,
    })


@image_imports_bp.post('/<uuid:item_id>/restore')
@cross_origin()
def restore_image_import(item_id):
    """在保留窗口内恢复已取消的导入项，重新排队生成向量。

    复用原已上传的暂存对象；窗口已过或对象已清理时拒绝。
    """
    try:
        item = db.session.execute(
            select(ImageImportItem)
            .where(ImageImportItem.id == item_id)
            .with_for_update()
        ).scalar_one_or_none()
        if item is None:
            db.session.rollback()
            return _error('图片导入项不存在', 'IMAGE_IMPORT_NOT_FOUND', 404)

        if item.status != 'cancelled':
            db.session.rollback()
            if item.status in ('queued', 'embedding', 'awaiting_retry'):
                return jsonify(item.to_public_dict())
            return _error(
                '仅保留窗口内的已取消导入项可恢复',
                'IMAGE_IMPORT_RESTORE_NOT_ALLOWED',
                409,
            )

        now = datetime.now()
        if item.objects_purged_at is not None or (
            item.purge_eligible_at is not None
            and item.purge_eligible_at <= now
        ):
            db.session.rollback()
            return _error(
                '保留窗口已过，暂存对象即将或已被清理，无法恢复',
                'IMAGE_IMPORT_RESTORE_WINDOW_EXPIRED',
                410,
            )

        before_state = {'status': 'cancelled'}
        item.status = 'queued'
        item.attempt_count = 0
        item.cancel_requested_at = None
        item.cancel_requested_by = None
        item.cancelled_at = None
        item.purge_eligible_at = None
        item.failed_at = None
        item.failure_message = None
        item.next_retry_at = None
        item.updated_at = now
        db.session.add(AssetActivityRecord(
            event_type='image_import.restored',
            target_type='image_import_item',
            target_id=str(item.id),
            task_id=str(item.id),
            request_id=uuid.uuid4().hex,
            source='api',
            before_state=before_state,
            after_state={'status': 'queued'},
            result='queued',
        ))
        db.session.commit()
        return jsonify(item.to_public_dict())
    except Exception:
        db.session.rollback()
        current_app.logger.error(
            '图片导入恢复失败 item_id=%s', item_id, exc_info=True
        )
        return _error('恢复失败', 'IMAGE_IMPORT_RESTORE_FAILED', 500)


@image_imports_bp.post('/<uuid:item_id>/abandon')
@cross_origin()
def abandon_image_import(item_id):
    """提前放弃已取消或失败的导入项：立即到期并进入清理，不可逆。"""
    try:
        item = db.session.execute(
            select(ImageImportItem)
            .where(ImageImportItem.id == item_id)
            .with_for_update()
        ).scalar_one_or_none()
        if item is None:
            db.session.rollback()
            return _error('图片导入项不存在', 'IMAGE_IMPORT_NOT_FOUND', 404)

        if item.status == 'abandoned':
            db.session.rollback()
            return jsonify(item.to_public_dict())

        if item.status not in ('cancelled', 'failed'):
            db.session.rollback()
            return _error(
                '仅已取消或失败的导入项可提前放弃',
                'IMAGE_IMPORT_ABANDON_NOT_ALLOWED',
                409,
            )

        if item.objects_purged_at is not None:
            db.session.rollback()
            return _error(
                '该导入的暂存对象已清理',
                'IMAGE_IMPORT_ABANDON_PURGED',
                410,
            )

        before_status = item.status
        now = datetime.now()
        item.status = 'abandoned'
        item.purge_eligible_at = now
        item.updated_at = now
        db.session.add(AssetActivityRecord(
            event_type='image_import.abandoned',
            target_type='image_import_item',
            target_id=str(item.id),
            task_id=str(item.id),
            request_id=uuid.uuid4().hex,
            source='api',
            before_state={'status': before_status},
            after_state={'status': 'abandoned'},
            result='abandoned',
        ))
        db.session.commit()
        return jsonify(item.to_public_dict())
    except Exception:
        db.session.rollback()
        current_app.logger.error(
            '图片导入放弃失败 item_id=%s', item_id, exc_info=True
        )
        return _error('放弃失败', 'IMAGE_IMPORT_ABANDON_FAILED', 500)

