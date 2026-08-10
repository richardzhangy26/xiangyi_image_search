"""图片资产活动记录的安全状态摘要。"""

from collections.abc import Mapping


def activity_state(asset) -> dict:
    """Return only audit-safe lifecycle fields for an image asset."""
    def value(name):
        if isinstance(asset, Mapping):
            return asset.get(name)
        return getattr(asset, name)

    return {
        'model_number': value('model_number'),
        'display_name': value('display_name'),
        'version': value('version'),
        'status': value('status'),
    }
