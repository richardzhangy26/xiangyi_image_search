"""独立图片资产 API。"""

import logging

from flask import Blueprint, current_app, jsonify, redirect

from models import ImageAsset, db
from services.object_storage import ObjectStorageError, OssObjectStorage

logger = logging.getLogger(__name__)

image_assets_bp = Blueprint(
    'image_assets',
    __name__,
    url_prefix='/api/image-assets',
)


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
