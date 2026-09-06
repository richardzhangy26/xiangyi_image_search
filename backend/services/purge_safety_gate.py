from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol


CONDITION_IDS = (
    "daily_postgres_backup",
    "instant_restore_point_capability",
    "object_protection",
    "independent_backup_credentials",
    "recovery_drill",
)

CONDITION_LABELS = {
    "daily_postgres_backup": "数据库定期备份",
    "instant_restore_point_capability": "即时备份能力",
    "object_protection": "对象保护",
    "independent_backup_credentials": "独立备份凭证",
    "recovery_drill": "恢复演练",
}

MAX_EVIDENCE_FILE_BYTES = 65536
FORBIDDEN_EVIDENCE_KEYS = frozenset({
    "password",
    "secret",
    "token",
    "authorization",
    "dsn",
})
_SUMMARY_MAX_CHARS = 200
_FUTURE_SKEW = timedelta(seconds=60)
CONDITION_MAX_AGES = {
    "daily_postgres_backup": timedelta(hours=26),
    "instant_restore_point_capability": timedelta(minutes=60),
    "object_protection": timedelta(hours=24),
    "independent_backup_credentials": timedelta(hours=24),
    "recovery_drill": timedelta(hours=24),
}


@dataclass(frozen=True)
class RawConditionEvidence:
    result: object = None
    verified_at: object = None
    expires_at: object = None
    summary: object = None
    parse_error: bool = False


@dataclass(frozen=True)
class ConditionSnapshot:
    id: str
    status: str
    checked_at: datetime | None
    expires_at: datetime | None
    summary: str | None


@dataclass(frozen=True)
class GateSnapshot:
    ready: bool
    checked_at: datetime
    conditions: tuple[ConditionSnapshot, ...]


class GateNotReady(Exception):
    error_code = "PURGE_GATE_NOT_READY"

    def __init__(self, snapshot: GateSnapshot):
        super().__init__("永久清除安全门未满足")
        self.snapshot = snapshot


class GateEvidenceSource(Protocol):
    def load(self, now: datetime) -> Mapping[str, RawConditionEvidence]:
        ...


def pipeline_available() -> bool:
    from flask import current_app
    from services.purge_pipeline_capability import (
        UnavailablePurgePipelineCapabilitySource,
    )

    source = current_app.config.get(
        'PURGE_PIPELINE_CAPABILITY_SOURCE',
        UnavailablePurgePipelineCapabilitySource(),
    )
    try:
        return bool(source.evaluate(datetime.now(timezone.utc)))
    except Exception:
        return False


def _contains_forbidden(value) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in FORBIDDEN_EVIDENCE_KEYS:
                return True
            if _contains_forbidden(nested):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_forbidden(item) for item in value)
    return False


def _parse_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _clip_summary(value) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:_SUMMARY_MAX_CHARS]


def _unknown(condition_id: str) -> ConditionSnapshot:
    return ConditionSnapshot(
        id=condition_id,
        status="unknown",
        checked_at=None,
        expires_at=None,
        summary=None,
    )


def _failed(condition_id: str, summary=None) -> ConditionSnapshot:
    return ConditionSnapshot(
        id=condition_id,
        status="failed",
        checked_at=None,
        expires_at=None,
        summary=_clip_summary(summary),
    )


def _classify(condition_id: str, raw: RawConditionEvidence | None, now: datetime) -> ConditionSnapshot:
    if raw is None:
        return _unknown(condition_id)
    if raw.parse_error or raw.result not in ("valid", "failed"):
        return _failed(condition_id)
    if raw.result == "failed":
        return _failed(condition_id, raw.summary)
    verified_at = _parse_datetime(raw.verified_at)
    expires_at = _parse_datetime(raw.expires_at)
    if verified_at is None or expires_at is None:
        return _failed(condition_id)
    if verified_at > now + _FUTURE_SKEW:
        return ConditionSnapshot(
            id=condition_id,
            status="unknown",
            checked_at=verified_at,
            expires_at=expires_at,
            summary=_clip_summary(raw.summary),
        )
    maximum_age = CONDITION_MAX_AGES[condition_id]
    if now - verified_at > maximum_age:
        return ConditionSnapshot(
            id=condition_id,
            status="expired",
            checked_at=verified_at,
            expires_at=expires_at,
            summary=_clip_summary(raw.summary),
        )
    if expires_at <= verified_at or expires_at - verified_at > maximum_age:
        return _failed(condition_id, raw.summary)
    if expires_at <= now:
        return ConditionSnapshot(
            id=condition_id,
            status="expired",
            checked_at=verified_at,
            expires_at=expires_at,
            summary=_clip_summary(raw.summary),
        )
    return ConditionSnapshot(
        id=condition_id,
        status="valid",
        checked_at=verified_at,
        expires_at=expires_at,
        summary=_clip_summary(raw.summary),
    )


class FileGateEvidenceSource:
    def __init__(self, evidence_dir: Path | None):
        self._dir = evidence_dir

    def load(self, now: datetime) -> Mapping[str, RawConditionEvidence]:
        if self._dir is None or not self._dir.is_dir():
            return {}
        loaded = {}
        for condition_id in CONDITION_IDS:
            path = self._dir / f"{condition_id}.json"
            if not path.is_file():
                continue
            loaded[condition_id] = self._read_file(path, condition_id)
        return loaded

    def _read_file(self, path: Path, condition_id: str) -> RawConditionEvidence:
        error = RawConditionEvidence(parse_error=True)
        try:
            if path.stat().st_size > MAX_EVIDENCE_FILE_BYTES:
                return error
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return error
        if not isinstance(payload, dict) or _contains_forbidden(payload):
            return error
        if payload.get("schema_version") != 1:
            return error
        if payload.get("condition") != condition_id:
            return error
        return RawConditionEvidence(
            result=payload.get("result"),
            verified_at=payload.get("verified_at"),
            expires_at=payload.get("expires_at"),
            summary=payload.get("summary"),
        )


class DictGateEvidenceSource:
    def __init__(self, records: Mapping[str, RawConditionEvidence]):
        self._records = dict(records)

    def load(self, now: datetime) -> Mapping[str, RawConditionEvidence]:
        return dict(self._records)


class PurgeSafetyGate:
    def __init__(
        self,
        source: GateEvidenceSource,
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self._source = source
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def evaluate(self, now: datetime | None = None) -> GateSnapshot:
        moment = now or self._clock()
        try:
            loaded = self._source.load(moment)
        except Exception:
            conditions = tuple(_unknown(condition_id) for condition_id in CONDITION_IDS)
            return GateSnapshot(ready=False, checked_at=moment, conditions=conditions)
        conditions = tuple(
            _classify(condition_id, loaded.get(condition_id), moment)
            for condition_id in CONDITION_IDS
        )
        ready = all(item.status == "valid" for item in conditions)
        return GateSnapshot(ready=ready, checked_at=moment, conditions=conditions)

    def require_ready(self, now: datetime | None = None) -> GateSnapshot:
        snapshot = self.evaluate(now)
        if not snapshot.ready:
            raise GateNotReady(snapshot)
        return snapshot
