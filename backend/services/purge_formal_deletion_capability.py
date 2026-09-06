"""Batch-bound, short-lived formal-deletion capability evidence.

This evidence is independent from the five backup gates and from the #26
``backup_only_no_delete`` heartbeat.  Reading a valid grant still does not
construct a deleter; the independent composition root owns that later step.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.purge_delete_trust import NoOverwriteTrustAttestation


MAX_GRANT_BYTES = 65536
MAX_GRANT_LIFETIME = timedelta(minutes=15)
FUTURE_SKEW = timedelta(seconds=60)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FORBIDDEN_KEY_PARTS = ("password", "secret", "token", "authorization", "dsn")
_EXPECTED_KEYS = {
    "schema_version",
    "result",
    "grant_id",
    "environment_id",
    "deployment_sha256",
    "batch_id",
    "asset_ids",
    "max_batches",
    "max_assets",
    "max_object_deletes",
    "database_manifest_sha256",
    "object_manifest_sha256",
    "formal_bucket",
    "issued_at",
    "expires_at",
    "trust",
}


@dataclass(frozen=True)
class FormalDeletionContext:
    environment_id: str
    deployment_sha256: str
    batch_id: uuid.UUID
    asset_ids: tuple[uuid.UUID, ...]
    database_manifest_sha256: str
    object_manifest_sha256: str
    formal_bucket: str

    def __post_init__(self):
        if not _IDENTIFIER.fullmatch(self.environment_id):
            raise ValueError("formal capability environment invalid")
        if not _SHA256.fullmatch(self.deployment_sha256):
            raise ValueError("formal capability deployment invalid")
        if not isinstance(self.batch_id, uuid.UUID):
            raise ValueError("formal capability batch invalid")
        if (
            not 1 <= len(self.asset_ids) <= 20
            or self.asset_ids != tuple(sorted(self.asset_ids))
            or len(set(self.asset_ids)) != len(self.asset_ids)
        ):
            raise ValueError("formal capability assets invalid")
        if not all(isinstance(value, uuid.UUID) for value in self.asset_ids):
            raise ValueError("formal capability asset identity invalid")
        if not _SHA256.fullmatch(self.database_manifest_sha256):
            raise ValueError("formal capability database evidence invalid")
        if not _SHA256.fullmatch(self.object_manifest_sha256):
            raise ValueError("formal capability object evidence invalid")
        if not _safe_bucket(self.formal_bucket):
            raise ValueError("formal capability Bucket invalid")


@dataclass(frozen=True)
class FormalDeletionGrant:
    grant_id: str
    context: FormalDeletionContext
    max_object_deletes: int
    issued_at: datetime
    expires_at: datetime
    trust_attestation_sha256: str


class UnavailableFormalDeletionCapabilitySource:
    def evaluate(self, *_args, **_kwargs) -> bool:
        return False


class FileFormalDeletionCapabilitySource:
    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = False,
        required_writer_inventory_sha256: str | None = None,
    ):
        self.path = Path(path)
        self.enabled = enabled is True
        self.required_writer_inventory_sha256 = required_writer_inventory_sha256

    def evaluate(self, context: FormalDeletionContext, *, now=None):
        if (
            not self.enabled
            or not isinstance(self.required_writer_inventory_sha256, str)
            or not _SHA256.fullmatch(self.required_writer_inventory_sha256)
        ):
            return None
        moment = _as_utc(now or datetime.now(timezone.utc))
        try:
            if (
                not self.path.is_file()
                or self.path.stat().st_size > MAX_GRANT_BYTES
            ):
                return None
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or set(payload) != _EXPECTED_KEYS
                or _contains_forbidden_key(payload)
                or payload.get("schema_version") != 1
                or payload.get("result") != "valid"
            ):
                return None
            parsed_context = _parse_context(payload)
            if parsed_context != context:
                return None
            if payload.get("max_batches") != 1:
                return None
            if payload.get("max_assets") != len(context.asset_ids):
                return None
            max_object_deletes = payload.get("max_object_deletes")
            if (
                type(max_object_deletes) is not int
                or not len(context.asset_ids) <= max_object_deletes <= 40
            ):
                return None
            issued_at = _parse_time(payload.get("issued_at"))
            expires_at = _parse_time(payload.get("expires_at"))
            if (
                expires_at <= issued_at
                or expires_at - issued_at > MAX_GRANT_LIFETIME
                or issued_at > moment + FUTURE_SKEW
                or expires_at <= moment
            ):
                return None
            grant_id = str(payload.get("grant_id") or "")
            if not _IDENTIFIER.fullmatch(grant_id):
                return None
            trust = NoOverwriteTrustAttestation.from_dict(payload.get("trust"))
            if (
                trust.writer_inventory_sha256
                != self.required_writer_inventory_sha256
            ):
                return None
            decision = trust.evaluate(
                now=moment,
                environment_id=context.environment_id,
                formal_bucket=context.formal_bucket,
            )
            if not decision.allowed:
                return None
            return FormalDeletionGrant(
                grant_id=grant_id,
                context=context,
                max_object_deletes=max_object_deletes,
                issued_at=issued_at,
                expires_at=expires_at,
                trust_attestation_sha256=decision.attestation_sha256,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return None


def _parse_context(payload):
    asset_values = payload.get("asset_ids")
    if not isinstance(asset_values, list):
        raise ValueError("formal capability assets invalid")
    return FormalDeletionContext(
        environment_id=str(payload.get("environment_id") or ""),
        deployment_sha256=str(payload.get("deployment_sha256") or ""),
        batch_id=uuid.UUID(str(payload.get("batch_id") or "")),
        asset_ids=tuple(uuid.UUID(str(value)) for value in asset_values),
        database_manifest_sha256=str(
            payload.get("database_manifest_sha256") or ""
        ),
        object_manifest_sha256=str(payload.get("object_manifest_sha256") or ""),
        formal_bucket=str(payload.get("formal_bucket") or ""),
    )


def _contains_forbidden_key(value):
    if isinstance(value, dict):
        return any(
            any(part in str(key).lower() for part in _FORBIDDEN_KEY_PARTS)
            or _contains_forbidden_key(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _parse_time(value):
    if not isinstance(value, str):
        raise ValueError("formal capability time invalid")
    return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _as_utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("formal capability time must include timezone")
    return value.astimezone(timezone.utc)


def _safe_bucket(value):
    return bool(
        isinstance(value, str)
        and value
        and "/" not in value
        and "\\" not in value
        and not any(ord(character) < 32 for character in value)
    )
