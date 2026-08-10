"""Issue #22 保留期策略的纯单元测试（可控时间，无真实服务）。"""

from __future__ import annotations

from datetime import datetime, timedelta

from services import import_retention


NOW = datetime(2026, 8, 10, 12, 0, 0)


def test_default_retention_windows_are_seven_and_thirty_days():
    assert import_retention.CANCEL_RETENTION_DAYS_DEFAULT == 7
    assert import_retention.FAILED_RETENTION_DAYS_DEFAULT == 30


def test_cancel_deadline_uses_cancel_window(monkeypatch):
    monkeypatch.delenv('IMPORT_CANCEL_RETENTION_DAYS', raising=False)
    cancelled_at = datetime(2026, 8, 10, 0, 0, 0)

    assert import_retention.cancel_purge_deadline(cancelled_at) == (
        cancelled_at + timedelta(days=7)
    )


def test_failed_deadline_uses_failed_window(monkeypatch):
    monkeypatch.delenv('IMPORT_FAILED_RETENTION_DAYS', raising=False)
    failed_at = datetime(2026, 8, 10, 0, 0, 0)

    assert import_retention.failed_purge_deadline(failed_at) == (
        failed_at + timedelta(days=30)
    )


def test_retention_windows_respect_environment_overrides(monkeypatch):
    monkeypatch.setenv('IMPORT_CANCEL_RETENTION_DAYS', '3')
    monkeypatch.setenv('IMPORT_FAILED_RETENTION_DAYS', '10')
    base = datetime(2026, 8, 10, 0, 0, 0)

    assert import_retention.cancel_purge_deadline(base) == base + timedelta(days=3)
    assert import_retention.failed_purge_deadline(base) == base + timedelta(days=10)


def test_invalid_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv('IMPORT_CANCEL_RETENTION_DAYS', 'not-a-number')
    monkeypatch.setenv('IMPORT_FAILED_RETENTION_DAYS', '-5')

    assert import_retention.cancel_retention_days() == 7
    assert import_retention.failed_retention_days() == 30


def test_purge_eligibility_matrix():
    eligible = dict(
        status='cancelled',
        purge_eligible_at=NOW - timedelta(hours=1),
        objects_purged_at=None,
        now=NOW,
    )
    assert import_retention.is_purge_eligible(**eligible) is True

    # 未到期的不可清理
    assert import_retention.is_purge_eligible(
        status='cancelled',
        purge_eligible_at=NOW + timedelta(hours=1),
        objects_purged_at=None,
        now=NOW,
    ) is False

    # 已清理过的幂等跳过
    assert import_retention.is_purge_eligible(
        status='cancelled',
        purge_eligible_at=NOW - timedelta(hours=1),
        objects_purged_at=NOW,
        now=NOW,
    ) is False

    # 活跃/处理中状态永不进入清理
    for status in ('queued', 'embedding', 'completed', 'awaiting_retry'):
        assert import_retention.is_purge_eligible(
            status=status,
            purge_eligible_at=NOW - timedelta(hours=1),
            objects_purged_at=None,
            now=NOW,
        ) is False

    # abandoned 立即到期可清理
    assert import_retention.is_purge_eligible(
        status='abandoned',
        purge_eligible_at=NOW,
        objects_purged_at=None,
        now=NOW,
    ) is True


def test_remaining_window_clamps_at_zero():
    assert import_retention.remaining_window(
        purge_eligible_at=NOW + timedelta(days=2), now=NOW
    ) == timedelta(days=2)
    assert import_retention.remaining_window(
        purge_eligible_at=NOW - timedelta(days=1), now=NOW
    ) == timedelta(0)
    assert import_retention.remaining_window(
        purge_eligible_at=None, now=NOW
    ) is None
