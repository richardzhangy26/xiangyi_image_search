"""独立图片资产 API。"""

import logging

from flask import Blueprint, current_app, jsonify, redirect, request

from models import ImageAsset, db
from services.object_storage import ObjectStorageError, OssObjectStorage

logger = logging.getLogger(__name__)

image_assets_bp = Blueprint(
    'image_assets',
    __name__,
    url_prefix='/api/image-assets',
)

MANAGEMENT_ASSIGNMENTS = frozenset({'unassigned', 'assigned', 'all'})


def _management_asset_dict(asset):
    """返回产品管理页所需的最小安全字段集合。"""
    return {
        'asset_id': str(asset.id),
        'model_number': asset.model_number,
        'source_relative_path': asset.source_relative_path,
        'preview_url': f'/api/image-assets/{asset.id}/preview',
        'source_size': asset.source_size,
        'source_mime_type': asset.source_mime_type,
        'source_width': asset.source_width,
        'source_height': asset.source_height,
        'created_at': asset.created_at.isoformat() if asset.created_at else None,
    }


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
        query = query.filter(ImageAsset.source_relative_path.ilike(f'%{search}%'))

    pagination = query.order_by(
        ImageAsset.created_at.desc(), ImageAsset.id.desc()
    ).paginate(page=page, per_page=per_page, error_out=False)
    return jsonify({
        'assets': [_management_asset_dict(asset) for asset in pagination.items],
        'total': pagination.total,
        'page': page,
        'per_page': per_page,
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

    try:
        signed_url = storage.sign_download_url(
            asset.preview_oss_path,
            current_app.config['OSS_SIGNED_URL_TTL_SECONDS'],
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

    return redirect(signed_url, code=302)
