"""图片资产显示名称领域规则与改名事务。"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Optional

from sqlalchemy import func, update

from services.asset_activity import activity_state


class DisplayNameValidationError(ValueError):
    """显示名称主体不满足稳定服务端约束。"""


def default_display_name(source_relative_path: str) -> str:
    """从不可变来源相对路径取得保留扩展名的默认显示名称。"""
    if not isinstance(source_relative_path, str):
        raise DisplayNameValidationError('来源相对路径无效')
    basename = PurePosixPath(source_relative_path).name
    if not basename:
        raise DisplayNameValidationError('来源相对路径缺少文件名')
    return basename


def normalize_name_body(value: object) -> str:
    """规范化用户可编辑主体；扩展名不属于该输入。"""
    if not isinstance(value, str):
        raise DisplayNameValidationError('显示名称主体必须是字符串')
    normalized = value.strip()
    if not 1 <= len(normalized) <= 100:
        raise DisplayNameValidationError('显示名称主体长度必须为 1 至 100 个字符')
    if normalized in {'.', '..'} or '/' in normalized or '\\' in normalized:
        raise DisplayNameValidationError('显示名称主体包含不允许的路径字符')
    if any(unicodedata.category(char) == 'Cc' for char in normalized):
        raise DisplayNameValidationError('显示名称主体包含控制字符')
    return normalized


def compose_display_name(source_relative_path: str, name_body: object) -> str:
    """由服务端来源扩展名与已校验主体组成完整显示名称。"""
    basename = default_display_name(source_relative_path)
    extension = PurePosixPath(basename).suffix
    return f'{normalize_name_body(name_body)}{extension}'


@dataclass(frozen=True)
class RenameAssetResult:
    status: str
    asset: Optional[dict] = None
    error_code: Optional[str] = None


def _value(asset, name):
    if isinstance(asset, Mapping):
        return asset.get(name)
    return getattr(asset, name)


def management_asset_dict(asset) -> dict:
    """Build the safe management representation shared by list and rename."""
    asset_id = _value(asset, 'id')
    created_at = _value(asset, 'created_at')
    if isinstance(asset, Mapping):
        archived_at = asset.get('archived_at')
    else:
        archived_at = getattr(asset, 'archived_at', None)
    return {
        'asset_id': str(asset_id),
        'model_number': _value(asset, 'model_number'),
        'display_name': _value(asset, 'display_name'),
        'source_relative_path': _value(asset, 'source_relative_path'),
        'version': _value(asset, 'version'),
        'status': _value(asset, 'status'),
        'archived_at': archived_at.isoformat() if archived_at else None,
        'preview_url': f'/api/image-assets/{asset_id}/preview',
        'source_size': _value(asset, 'source_size'),
        'source_mime_type': _value(asset, 'source_mime_type'),
        'source_width': _value(asset, 'source_width'),
        'source_height': _value(asset, 'source_height'),
        'created_at': created_at.isoformat() if created_at else None,
    }


def rename_image_asset(
    session,
    asset_id,
    *,
    name_body: object,
    expected_version: int,
    request_id: str,
) -> RenameAssetResult:
    """Atomically rename one active asset and persist its activity record."""
    from models import AssetActivityRecord, ImageAsset

    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        raise DisplayNameValidationError('expected_version 必须是正整数')

    current = session.get(ImageAsset, asset_id)
    if current is None:
        return RenameAssetResult(
            'not_found', error_code='IMAGE_ASSET_NOT_FOUND'
        )
    if current.status != 'active':
        return RenameAssetResult(
            'not_active',
            management_asset_dict(current),
            'IMAGE_ASSET_NOT_ACTIVE',
        )
    if current.version != expected_version:
        return RenameAssetResult(
            'conflict',
            management_asset_dict(current),
            'IMAGE_ASSET_VERSION_CONFLICT',
        )

    full_name = compose_display_name(current.source_relative_path, name_body)
    before_state = activity_state(current)
    statement = (
        update(ImageAsset)
        .where(
            ImageAsset.id == asset_id,
            ImageAsset.status == 'active',
            ImageAsset.version == expected_version,
        )
        .values(
            display_name=full_name,
            version=ImageAsset.version + 1,
            updated_at=func.now(),
        )
        .returning(
            ImageAsset.id,
            ImageAsset.model_number,
            ImageAsset.display_name,
            ImageAsset.source_relative_path,
            ImageAsset.version,
            ImageAsset.status,
            ImageAsset.source_size,
            ImageAsset.source_mime_type,
            ImageAsset.source_width,
            ImageAsset.source_height,
            ImageAsset.created_at,
        )
        .execution_options(synchronize_session=False)
    )

    try:
        updated = session.execute(statement).mappings().one_or_none()
        if updated is None:
            session.expire_all()
            latest = session.get(ImageAsset, asset_id)
            if latest is None:
                return RenameAssetResult(
                    'not_found', error_code='IMAGE_ASSET_NOT_FOUND'
                )
            if latest.status != 'active':
                return RenameAssetResult(
                    'not_active',
                    management_asset_dict(latest),
                    'IMAGE_ASSET_NOT_ACTIVE',
                )
            return RenameAssetResult(
                'conflict',
                management_asset_dict(latest),
                'IMAGE_ASSET_VERSION_CONFLICT',
            )

        session.add(AssetActivityRecord(
            event_type='asset.rename',
            target_type='image_asset',
            target_id=str(asset_id),
            request_id=request_id[:64],
            source='api',
            before_state=before_state,
            after_state=activity_state(updated),
            result='succeeded',
        ))
        session.commit()
        return RenameAssetResult('renamed', management_asset_dict(updated))
    except Exception:
        session.rollback()
        raise
