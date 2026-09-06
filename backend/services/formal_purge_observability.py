"""Vendor-neutral formal-purge health evidence and safe operational events."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


HEALTH_TTL = timedelta(seconds=120)
MAX_HEALTH_BYTES = 65536
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EVENT = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_EXPECTED_HEALTH_KEYS = {
    "schema_version", "component", "result", "checked_at", "expires_at",
    "environment_id", "batch_id", "stage", "error_code",
}


@dataclass(frozen=True)
class FormalPurgeHealthSnapshot:
    available: bool
    result: str | None = None
    checked_at: datetime | None = None
    expires_at: datetime | None = None
    environment_id: str | None = None
    batch_id: uuid.UUID | None = None
    stage: str | None = None
    error_code: str | None = None


class FileFormalPurgeHealthSource:
    def __init__(self, path: Path):
        self.path = Path(path)

    def evaluate(self, *, now=None):
        moment = _as_utc(now or datetime.now(timezone.utc))
        unavailable = FormalPurgeHealthSnapshot(available=False)
        try:
            if (
                not self.path.is_file()
                or self.path.stat().st_size > MAX_HEALTH_BYTES
            ):
                return unavailable
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or set(payload) != _EXPECTED_HEALTH_KEYS
                or payload.get("schema_version") != 1
                or payload.get("component") != "formal_purge_worker"
                or payload.get("result") not in {"valid", "failed", "disabled"}
            ):
                return unavailable
            checked_at = _parse_time(payload.get("checked_at"))
            expires_at = _parse_time(payload.get("expires_at"))
            if (
                expires_at <= checked_at
                or expires_at > checked_at + HEALTH_TTL
                or expires_at <= moment
                or checked_at > moment + timedelta(seconds=60)
            ):
                return unavailable
            environment_id = str(payload.get("environment_id") or "")
            if not _IDENTIFIER.fullmatch(environment_id):
                return unavailable
            batch_id = uuid.UUID(str(payload.get("batch_id")))
            stage = str(payload.get("stage") or "")
            error_code = payload.get("error_code")
            if not _IDENTIFIER.fullmatch(stage):
                return unavailable
            if error_code is not None and not _IDENTIFIER.fullmatch(str(error_code)):
                return unavailable
            return FormalPurgeHealthSnapshot(
                available=payload["result"] == "valid",
                result=payload["result"],
                checked_at=checked_at,
                expires_at=expires_at,
                environment_id=environment_id,
                batch_id=batch_id,
                stage=stage,
                error_code=str(error_code) if error_code is not None else None,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return unavailable


def write_formal_purge_health(
    path: Path,
    *,
    now: datetime,
    result: str,
    environment_id: str,
    batch_id: uuid.UUID,
    stage: str,
    error_code: str | None,
):
    moment = _as_utc(now)
    if result not in {"valid", "failed", "disabled"}:
        raise ValueError("formal health result invalid")
    if not _IDENTIFIER.fullmatch(environment_id) or not _IDENTIFIER.fullmatch(stage):
        raise ValueError("formal health identity invalid")
    if not isinstance(batch_id, uuid.UUID):
        raise ValueError("formal health batch invalid")
    if error_code is not None and not _IDENTIFIER.fullmatch(error_code):
        raise ValueError("formal health error code invalid")
    payload = {
        "schema_version": 1,
        "component": "formal_purge_worker",
        "result": result,
        "checked_at": moment.isoformat(),
        "expires_at": (moment + HEALTH_TTL).isoformat(),
        "environment_id": environment_id,
        "batch_id": str(batch_id),
        "stage": stage,
        "error_code": error_code,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)


@dataclass(frozen=True)
class FormalPurgeOperationalEvent:
    event_type: str
    occurred_at: datetime
    environment_id: str
    batch_id: uuid.UUID
    target_asset_id: uuid.UUID | None
    checkpoint: str | None
    result: str
    error_code: str | None

    def to_dict(self):
        if not _EVENT.fullmatch(self.event_type):
            raise ValueError("formal event type invalid")
        if not _IDENTIFIER.fullmatch(self.environment_id):
            raise ValueError("formal event environment invalid")
        if self.result not in {"succeeded", "failed", "denied", "disabled"}:
            raise ValueError("formal event result invalid")
        if self.checkpoint is not None and not _IDENTIFIER.fullmatch(self.checkpoint):
            raise ValueError("formal event checkpoint invalid")
        if self.error_code is not None and not _IDENTIFIER.fullmatch(self.error_code):
            raise ValueError("formal event error code invalid")
        return {
            "schema_version": 1,
            "event_type": self.event_type,
            "occurred_at": _as_utc(self.occurred_at).isoformat(),
            "environment_id": self.environment_id,
            "batch_id": str(self.batch_id),
            "target_asset_id": (
                str(self.target_asset_id) if self.target_asset_id else None
            ),
            "checkpoint": self.checkpoint,
            "result": self.result,
            "error_code": self.error_code,
        }


class JsonFormalPurgeEventSink:
    def __init__(self, write_line):
        self._write_line = write_line

    def emit(self, event: FormalPurgeOperationalEvent):
        if not isinstance(event, FormalPurgeOperationalEvent):
            raise TypeError("typed formal event required")
        self._write_line(
            json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"))
        )


def _parse_time(value):
    if not isinstance(value, str):
        raise ValueError("formal health time invalid")
    return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _as_utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("formal health time must include timezone")
    return value.astimezone(timezone.utc)
