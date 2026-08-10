"""导入项保留期策略：窗口计算、到期判定与剩余时长。

全部为纯函数与常量：不访问数据库、对象存储或模型服务，可被可控时间测试覆盖。
窗口语义（Issue #22）：
- 取消项保留 7 天，窗口内可恢复导入；
- 自动重试耗尽的失败项保留 30 天，手工重试重新计算窗口；
- 提前放弃（abandoned）立即到期。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta


CANCEL_RETENTION_DAYS_DEFAULT = 7
FAILED_RETENTION_DAYS_DEFAULT = 30

PURGE_ELIGIBLE_STATUSES = ('cancelled', 'failed', 'abandoned')


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def cancel_retention_days() -> int:
    return _positive_int_env(
        'IMPORT_CANCEL_RETENTION_DAYS', CANCEL_RETENTION_DAYS_DEFAULT
    )


def failed_retention_days() -> int:
    return _positive_int_env(
        'IMPORT_FAILED_RETENTION_DAYS', FAILED_RETENTION_DAYS_DEFAULT
    )


def cancel_purge_deadline(cancelled_at: datetime) -> datetime:
    """取消项的清理到期时刻 = 取消时刻 + 取消保留窗口。"""
    return cancelled_at + timedelta(days=cancel_retention_days())


def failed_purge_deadline(failed_at: datetime) -> datetime:
    """重试耗尽失败项的清理到期时刻 = 失败时刻 + 失败保留窗口。"""
    return failed_at + timedelta(days=failed_retention_days())


def is_purge_eligible(
    *,
    status: str,
    purge_eligible_at: datetime | None,
    objects_purged_at: datetime | None,
    now: datetime,
) -> bool:
    """只有终态项在到期后且尚未清理过时才可进入清理。"""
    return (
        status in PURGE_ELIGIBLE_STATUSES
        and purge_eligible_at is not None
        and objects_purged_at is None
        and purge_eligible_at <= now
    )


def remaining_window(
    *,
    purge_eligible_at: datetime | None,
    now: datetime,
) -> timedelta | None:
    """距离清理到期的剩余时长；已到期收敛为零，无窗口返回 None。"""
    if purge_eligible_at is None:
        return None
    remaining = purge_eligible_at - now
    return remaining if remaining > timedelta(0) else timedelta(0)
