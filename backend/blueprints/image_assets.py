"""独立图片资产 API。"""

import json
import logging
import time
import uuid

from flask import Blueprint, current_app, jsonify, redirect, request
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import RequestEntityTooLarge

from blueprints.products_v2 import ALLOWED_EXTENSIONS, asset_ingest_error_response
from models import ImageAsset, Product, db
from product_search import EmbeddingServiceError
from services.asset_display_name import (
    DisplayNameValidationError,
    management_asset_dict,
    rename_image_asset,
)
from services.asset_archive import (
    ArchiveRequestValidationError,
    archive_unassigned_image_assets,
)
from services.asset_ingest import (
    AssetIngestError,
    ImageAssetIngestService,
)
from services.asset_recycle_bin import (
    RestoreBlockedByPurgeBatch,
    RestoreRequestValidationError,
    list_archived_image_assets,
    restore_image_assets,
)
from services.image_normalizer import ImageNormalizationError
from services.object_source import InMemoryObjectSource
from services.object_storage import ObjectStorageError, OssObjectStorage

logger = logging.getLogger(__name__)

image_assets_bp = Blueprint(
    'image_assets',
    __name__,
    url_prefix='/api/image-assets',
)

MANAGEMENT_ASSIGNMENTS = frozenset({'unassigned', 'assigned', 'all'})
MAX_ASSIGNMENT_BATCH = 100
MAX_MODEL_NUMBER_LENGTH = 100

# 本地导入（单图/文件夹/剪贴板）以独立来源身份进入待归款列表，
# 不创建产品记录；正式原图与预览仍由统一入库服务写入私有 OSS。
IMPORT_SOURCE_PROVIDER = 'local-import'
IMPORT_SOURCE_BUCKET = 'user-imports'
DEFAULT_IMPORT_PREFIX = '手动导入'
MAX_IMPORT_BATCH = 20
MAX_IMPORT_PREFIX_LENGTH = 100
MAX_IMPORT_PATH_LENGTH = 512


def _management_asset_dict(asset):
    """返回产品管理页所需的最小安全字段集合。"""
    return management_asset_dict(asset)


def _literal_ilike_pattern(value):
    escaped = value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{escaped}%'


def _request_integer(name, default, minimum, maximum):
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if minimum <= value <= maximum else None


@image_assets_bp.get('')
def list_image_assets():
    assignment = request.args.get('assignment', 'unassigned')
    page = _request_integer('page', 1, 1, 1_000_000)
    per_page = _request_integer('per_page', 24, 1, 100)
    if assignment not in MANAGEMENT_ASSIGNMENTS or page is None or per_page is None:
        return jsonify({
            'error': '图片资产列表参数无效',
            'error_code': 'INVALID_IMAGE_ASSET_LIST_PARAMS',
        }), 400

    query = ImageAsset.query.filter(ImageAsset.status == 'active')
    if assignment == 'unassigned':
        query = query.filter(ImageAsset.model_number.is_(None))
    elif assignment == 'assigned':
        query = query.filter(ImageAsset.model_number.isnot(None))

    search = (request.args.get('search') or '').strip()
    if search:
        pattern = _literal_ilike_pattern(search)
        query = query.filter(or_(
            ImageAsset.display_name.ilike(pattern, escape='\\'),
            ImageAsset.source_relative_path.ilike(pattern, escape='\\'),
        ))

    pagination = query.order_by(
        ImageAsset.created_at.desc(), ImageAsset.id.desc()
    ).paginate(page=page, per_page=per_page, error_out=False)
    return jsonify({
        'assets': [_management_asset_dict(asset) for asset in pagination.items],
        'total': pagination.total,
        'page': page,
        'per_page': per_page,
    })


@image_assets_bp.get('/archived')
def list_archived_assets():
    page = _request_integer('page', 1, 1, 1_000_000)
    per_page = _request_integer('per_page', 24, 1, 100)
    if page is None or per_page is None:
        return jsonify({
            'error': '回收站列表参数无效',
            'error_code': 'INVALID_IMAGE_ASSET_ARCHIVED_LIST_PARAMS',
        }), 400

    request_id = (request.headers.get('X-Request-ID') or str(uuid.uuid4()))[:64]
    try:
        result = list_archived_image_assets(
            db.session,
            page=page,
            per_page=per_page,
            search=(request.args.get('search') or '').strip(),
        )
    except Exception as exc:
        db.session.rollback()
        logger.error(
            'image_asset.archived_list.failed request_id=%s error_type=%s',
            request_id,
            type(exc).__name__,
        )
        return jsonify({
            'error': '回收站加载失败，请稍后重试',
            'error_code': 'IMAGE_ASSET_ARCHIVED_LIST_FAILED',
        }), 500

    return jsonify({
        'assets': result.assets,
        'total': result.total,
        'archived_total': result.archived_total,
        'page': result.page,
        'per_page': result.per_page,
    })


@image_assets_bp.post('/<uuid:asset_id>/rename')
def rename_image_asset_display_name(asset_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({
            'error': '改名参数无效',
            'error_code': 'INVALID_IMAGE_ASSET_DISPLAY_NAME',
        }), 400

    request_id = (request.headers.get('X-Request-ID') or str(uuid.uuid4()))[:64]
    try:
        result = rename_image_asset(
            db.session,
            asset_id,
            name_body=payload.get('name_body'),
            expected_version=payload.get('expected_version'),
            request_id=request_id,
        )
    except DisplayNameValidationError as exc:
        return jsonify({
            'error': str(exc),
            'error_code': 'INVALID_IMAGE_ASSET_DISPLAY_NAME',
        }), 400
    except Exception as exc:
        logger.error(
            'image_asset.rename.failed asset_id=%s request_id=%s error_type=%s',
            asset_id,
            request_id,
            type(exc).__name__,
        )
        return jsonify({
            'error': '图片资产改名失败，请稍后重试',
            'error_code': 'IMAGE_ASSET_RENAME_FAILED',
        }), 500

    if result.status == 'renamed':
        return jsonify({'asset': result.asset})
    if result.status == 'not_found':
        return jsonify({
            'error': '图片资产不存在',
            'error_code': result.error_code,
        }), 404
    if result.status == 'not_active':
        return jsonify({
            'error': '归档图片需先恢复后才能改名',
            'error_code': result.error_code,
            'latest': result.asset,
        }), 409
    return jsonify({
        'error': '图片名称已被其他操作更新，请确认最新值后重试',
        'error_code': result.error_code,
        'latest': result.asset,
    }), 409


@image_assets_bp.post('/archive')
def archive_image_assets():
    """Move a bounded batch of unassigned assets out of discovery results."""
    payload = request.get_json(silent=True)
    request_id = (request.headers.get('X-Request-ID') or str(uuid.uuid4()))[:64]
    try:
        result = archive_unassigned_image_assets(
            db.session,
            payload.get('asset_ids') if isinstance(payload, dict) else None,
            request_id=request_id,
        )
    except ArchiveRequestValidationError as exc:
        db.session.rollback()
        return jsonify({'error': str(exc), 'error_code': exc.error_code}), 400
    except Exception as exc:
        db.session.rollback()
        logger.error(
            'image_asset.archive.failed request_id=%s error_type=%s',
            request_id,
            type(exc).__name__,
        )
        return jsonify({
            'error': '图片移入回收站失败，请稍后重试',
            'error_code': 'IMAGE_ASSET_ARCHIVE_FAILED',
        }), 500

    response = result.to_dict()
    if result.status == 'succeeded':
        return jsonify(response), 200
    response.update({
        'error': '部分图片资产不符合移入回收站条件，未修改本批数据',
        'error_code': 'IMAGE_ASSET_ARCHIVE_CONFLICT',
    })
    return jsonify(response), 409


@image_assets_bp.post('/restore')
def restore_archived_image_assets():
    payload = request.get_json(silent=True)
    request_id = (request.headers.get('X-Request-ID') or str(uuid.uuid4()))[:64]
    try:
        result = restore_image_assets(
            db.session,
            payload.get('asset_ids') if isinstance(payload, dict) else None,
            request_id=request_id,
        )
    except RestoreRequestValidationError as exc:
        db.session.rollback()
        return jsonify({
            'error': str(exc),
            'error_code': 'INVALID_IMAGE_ASSET_RESTORE_BATCH',
        }), 400
    except RestoreBlockedByPurgeBatch as exc:
        return jsonify({
            'error': str(exc),
            'error_code': exc.error_code,
            'batch_id': str(exc.batch_id),
        }), 409
    except Exception as exc:
        db.session.rollback()
        logger.error(
            'image_asset.restore.failed request_id=%s error_type=%s',
            request_id,
            type(exc).__name__,
        )
        return jsonify({
            'error': '图片恢复失败，请稍后重试',
            'error_code': 'IMAGE_ASSET_RESTORE_FAILED',
        }), 500

    response = result.to_dict()
    if result.status == 'succeeded':
        return jsonify(response), 200
    response.update({
        'error': '部分图片资产不符合恢复条件，未修改本批数据',
        'error_code': 'IMAGE_ASSET_RESTORE_CONFLICT',
    })
    return jsonify(response), 409


def _assignment_error(message, error_code, status):
    db.session.rollback()
    return jsonify({'error': message, 'error_code': error_code}), status


def _import_error(message, error_code, status):
    return jsonify({'error': message, 'error_code': error_code}), status


def _clean_import_path(raw, max_length):
    """清洗用户提供的导入路径段；拒绝穿越、绝对路径与非法字符。"""
    if not isinstance(raw, str):
        return None
    value = raw.replace('\x00', '').replace('\\', '/').strip()
    if not value or value.startswith('/') or len(value) > max_length:
        return None
    segments = []
    for segment in value.split('/'):
        segment = segment.strip()
        if not segment or segment == '.':
            continue
        if segment == '..':
            return None
        segments.append(segment)
    if not segments:
        return None
    return '/'.join(segments)


def _allowed_import_filename(path):
    name = path.rsplit('/', 1)[-1]
    return (
        '.' in name
        and name.rsplit('.', 1)[-1].lower()
        in {suffix.lstrip('.') for suffix in ALLOWED_EXTENSIONS}
    )


def _import_ingest_service(source):
    """构造本地导入专用的入库服务；测试替身注入点与产品上传一致。"""
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


def _import_item_error(result):
    """把入库单项失败转换为脱敏的用户可见原因。"""
    if result.status == 'source_conflict':
        return '来源冲突：同一路径已存在不同内容的图片'
    if result.error_stage == 'preview':
        return '图片已损坏或无法安全解码'
    if result.error_stage == 'embedding':
        return '图片识别服务暂不可用，请稍后重试该图片'
    return '导入失败，请稍后重试该图片'


_IMPORT_ITEM_STATUSES = frozenset({
    'created',
    'existing',
    'source_conflict',
    'in_recycle_bin',
    'failed',
})


def _import_result_item(result):
    status = (
        result.status
        if result.status in _IMPORT_ITEM_STATUSES
        else 'failed'
    )
    return {
        'relative_path': result.source_relative_path,
        'status': status,
        'asset_id': result.asset_id,
        'error': (
            _import_item_error(result)
            if status in {'source_conflict', 'failed'}
            else None
        ),
        'recovery_action': (
            result.recovery_action
            if status == 'in_recycle_bin'
            else None
        ),
    }


def _import_result_counts(items):
    counts = {
        'created_count': 0,
        'existing_count': 0,
        'conflict_count': 0,
        'recycle_bin_count': 0,
        'failed_count': 0,
        'skipped_count': 0,
    }
    count_key_by_status = {
        'created': 'created_count',
        'existing': 'existing_count',
        'source_conflict': 'conflict_count',
        'in_recycle_bin': 'recycle_bin_count',
        'failed': 'failed_count',
    }
    for item in items:
        counts[count_key_by_status[item['status']]] += 1
    return counts


@image_assets_bp.post('/import')
def import_unassigned_assets():
    """把本地图片批量导入为未归款资产，不创建产品记录。"""
    try:
        files = request.files.getlist('images')
    except RequestEntityTooLarge:
        db.session.rollback()
        return _import_error('上传图片过大', 'IMAGE_TOO_LARGE', 413)
    if not 1 <= len(files) <= MAX_IMPORT_BATCH:
        return _import_error(
            f'一次最多导入 {MAX_IMPORT_BATCH} 张图片',
            'INVALID_IMAGE_ASSET_IMPORT',
            400,
        )

    try:
        relative_paths = json.loads(request.form.get('relative_paths') or '[]')
    except (TypeError, ValueError):
        return _import_error(
            '导入参数无效', 'INVALID_IMAGE_ASSET_IMPORT', 400
        )
    if (
        not isinstance(relative_paths, list)
        or len(relative_paths) != len(files)
        or any(not isinstance(item, str) for item in relative_paths)
    ):
        return _import_error(
            '导入参数无效', 'INVALID_IMAGE_ASSET_IMPORT', 400
        )

    prefix = _clean_import_path(
        request.form.get('prefix') or DEFAULT_IMPORT_PREFIX,
        MAX_IMPORT_PREFIX_LENGTH,
    )
    if prefix is None:
        return _import_error(
            '导入命名前缀无效', 'INVALID_IMAGE_ASSET_IMPORT', 400
        )

    cleaned_paths = []
    for raw_path in relative_paths:
        cleaned = _clean_import_path(raw_path, MAX_IMPORT_PATH_LENGTH)
        if (
            cleaned is None
            or len(f'{prefix}/{cleaned}') > MAX_IMPORT_PATH_LENGTH
            or not _allowed_import_filename(cleaned)
        ):
            return _import_error(
                '导入路径无效：只支持 png/jpg/jpeg/gif/webp 图片的相对路径',
                'INVALID_IMAGE_ASSET_IMPORT',
                400,
            )
        cleaned_paths.append(f'{prefix}/{cleaned}')
    if len(set(cleaned_paths)) != len(cleaned_paths):
        return _import_error(
            '本批导入路径存在重复，请修改后重试',
            'INVALID_IMAGE_ASSET_IMPORT',
            400,
        )

    try:
        objects = {}
        content_types = {}
        for image_file, final_path in zip(files, cleaned_paths):
            objects[final_path] = image_file.read()
            content_types[final_path] = (
                image_file.mimetype or 'application/octet-stream'
            )
        source = InMemoryObjectSource(
            source_bucket=IMPORT_SOURCE_BUCKET,
            objects=objects,
            content_types=content_types,
        )
        results = _import_ingest_service(source).ingest_many(
            cleaned_paths,
            model_number=None,
            request_id=uuid.uuid4().hex,
        )
        if len(results) != len(cleaned_paths):
            raise AssetIngestError(
                '批量导入结果数量与请求不一致',
                stage='ingest',
            )
        items = [_import_result_item(result) for result in results]
        return jsonify({'items': items, **_import_result_counts(items)})
    except ImageNormalizationError:
        db.session.rollback()
        return _import_error(
            '上传图片已损坏或无法安全解码', 'INVALID_IMAGE', 400
        )
    except EmbeddingServiceError:
        db.session.rollback()
        logger.error('本地导入失败（向量服务）')
        return _import_error(
            '图片识别服务暂不可用，请稍后重试',
            'EMBEDDING_SERVICE_ERROR',
            503,
        )
    except ObjectStorageError:
        db.session.rollback()
        logger.error('本地导入失败（对象存储）')
        return _import_error(
            '图片存储服务暂不可用，请稍后重试',
            'OBJECT_STORAGE_ERROR',
            503,
        )
    except AssetIngestError as exc:
        db.session.rollback()
        logger.error(
            '本地导入失败（图片资产） stage=%s error_type=%s',
            exc.stage,
            type(exc).__name__,
        )
        return asset_ingest_error_response(exc)
    except Exception:
        db.session.rollback()
        logger.exception('本地导入失败')
        return _import_error(
            '图片导入失败', 'IMAGE_ASSET_IMPORT_FAILED', 500
        )


@image_assets_bp.post('/assign')
def assign_image_assets():
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get('asset_ids')
    model_number = payload.get('model_number')
    create_if_missing = payload.get('create_if_missing', False)
    if (
        not isinstance(raw_ids, list)
        or not 1 <= len(raw_ids) <= MAX_ASSIGNMENT_BATCH
        or not isinstance(model_number, str)
        or not model_number.strip()
        or not isinstance(create_if_missing, bool)
        or any(not isinstance(value, str) for value in raw_ids)
        or len(set(raw_ids)) != len(raw_ids)
    ):
        return _assignment_error(
            '关联参数无效', 'INVALID_IMAGE_ASSET_ASSIGNMENT', 400
        )

    try:
        asset_ids = [uuid.UUID(value) for value in raw_ids]
    except (TypeError, ValueError, AttributeError):
        return _assignment_error(
            '图片资产 ID 无效', 'INVALID_IMAGE_ASSET_ASSIGNMENT', 400
        )
    if len(set(asset_ids)) != len(asset_ids):
        return _assignment_error(
            '图片资产 ID 重复', 'INVALID_IMAGE_ASSET_ASSIGNMENT', 400
        )

    model_number = model_number.strip()
    if len(model_number) > MAX_MODEL_NUMBER_LENGTH:
        return _assignment_error(
            '关联参数无效', 'INVALID_IMAGE_ASSET_ASSIGNMENT', 400
        )

    # 锁定产品行使同一型号的归款串行化，避免并发追加产生并列 sort_order。
    product_created = False
    if db.session.get(Product, model_number, with_for_update=True) is None:
        if not create_if_missing:
            return _assignment_error(
                '目标型号不存在，请刷新产品列表', 'PRODUCT_NOT_FOUND', 404
            )
        # 快速创建只保留型号；NOT NULL 字段用空字符串占位，稍后在产品视图补全。
        db.session.add(Product(
            model_number=model_number,
            photographer_file='',
            alibaba_product_url='',
            category='',
        ))
        product_created = True

    assets = ImageAsset.query.filter(
        ImageAsset.id.in_(asset_ids)
    ).order_by(ImageAsset.id).with_for_update().all()
    if len(assets) != len(asset_ids):
        return _assignment_error(
            '图片资产不存在', 'IMAGE_ASSET_NOT_FOUND', 404
        )
    if any(asset.status != 'active' for asset in assets):
        return _assignment_error(
            '归档图片不能关联型号', 'IMAGE_ASSET_NOT_ACTIVE', 409
        )
    if any(asset.model_number not in (None, model_number) for asset in assets):
        return _assignment_error(
            '图片已关联其他型号，未修改本批数据',
            'IMAGE_ASSET_ASSIGNMENT_CONFLICT',
            409,
        )

    next_order = db.session.query(
        func.coalesce(func.max(ImageAsset.sort_order), -1)
    ).filter(
        ImageAsset.model_number == model_number,
        ImageAsset.status == 'active',
    ).scalar() + 1

    assets_by_id = {asset.id: asset for asset in assets}
    assigned_count = 0
    reused_count = 0
    for requested_id in asset_ids:
        asset = assets_by_id[requested_id]
        if asset.model_number == model_number:
            reused_count += 1
        else:
            asset.model_number = model_number
            asset.sort_order = next_order
            next_order += 1
            assigned_count += 1
    try:
        db.session.commit()
    except IntegrityError:
        return _assignment_error(
            '型号已存在，请刷新产品列表', 'PRODUCT_ALREADY_EXISTS', 409
        )
    return jsonify({
        'model_number': model_number,
        'assigned_count': assigned_count,
        'reused_count': reused_count,
        'product_created': product_created,
    })


@image_assets_bp.get('/<uuid:asset_id>/preview')
def private_preview(asset_id):
    asset = db.session.get(ImageAsset, asset_id)
    if asset is None or not asset.preview_oss_path:
        return jsonify({
            'error': '图片资产不存在',
            'error_code': 'IMAGE_ASSET_NOT_FOUND',
        }), 404

    storage = current_app.config.get('IMAGE_ASSET_STORAGE')
    if storage is None:
        try:
            storage = OssObjectStorage.from_env()
        except ObjectStorageError as exc:
            logger.error(
                'image_asset.preview.storage_unavailable asset_id=%s error_type=%s',
                asset_id,
                type(exc).__name__,
            )
            return jsonify({
                'error': '私有预览服务暂不可用',
                'error_code': 'PREVIEW_SIGNING_ERROR',
            }), 503

    ttl_seconds = current_app.config['OSS_SIGNED_URL_TTL_SECONDS']
    try:
        signed = storage.sign_download_url(
            asset.preview_oss_path,
            ttl_seconds,
            cache_control=f'private, max-age={ttl_seconds}',
        )
    except Exception as exc:  # 外部边界错误统一脱敏
        logger.error(
            'image_asset.preview.sign_failed asset_id=%s error_type=%s',
            asset_id,
            type(exc).__name__,
        )
        return jsonify({
            'error': '私有预览服务暂不可用',
            'error_code': 'PREVIEW_SIGNING_ERROR',
        }), 503

    # 签名 URL 在 TTL 时间窗口内保持稳定，允许浏览器同时缓存 302 跳转与
    # OSS 响应；缓存时长取窗口剩余时间并预留 30 秒余量，保证缓存期内
    # 签名始终有效，窗口内刷新页面不再消耗 OSS 出口流量。
    remaining = max(signed.expires_at - int(time.time()) - 30, 0)
    response = redirect(signed.url, code=302)
    response.headers['Cache-Control'] = (
        f'private, max-age={min(remaining, ttl_seconds)}'
    )
    return response
