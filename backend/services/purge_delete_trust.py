"""Fail-closed no-overwrite trust attestation for formal deletion.

The attestation does not grant deletion.  It only proves that the selected T14
trust model was reviewed for one environment/Bucket and one fenced-writer
inventory.  A separate batch-bound capability grant is still mandatory.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping


TRUST_MODE = "fenced_writers_iam_no_overwrite"
MAX_TRUST_AGE = timedelta(hours=24)
FUTURE_SKEW = timedelta(seconds=60)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ENVIRONMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EXPECTED_KEYS = {
    "schema_version",
    "mode",
    "result",
    "environment_id",
    "formal_bucket",
    "iam_policy_sha256",
    "writer_inventory_sha256",
    "verified_at",
    "expires_at",
}


@dataclass(frozen=True)
class NoOverwriteTrustDecision:
    allowed: bool
    error_code: str | None
    attestation_sha256: str


@dataclass(frozen=True)
class NoOverwriteTrustAttestation:
    environment_id: str
    formal_bucket: str
    iam_policy_sha256: str
    writer_inventory_sha256: str
    verified_at: datetime
    expires_at: datetime

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]):
        if not isinstance(payload, Mapping) or set(payload) != _EXPECTED_KEYS:
            raise ValueError("trust attestation fields invalid")
        if payload.get("schema_version") != 1:
            raise ValueError("trust schema invalid")
        if payload.get("mode") != TRUST_MODE:
            raise ValueError("trust mode invalid")
        if payload.get("result") != "valid":
            raise ValueError("trust result invalid")
        environment_id = str(payload.get("environment_id") or "")
        formal_bucket = str(payload.get("formal_bucket") or "")
        iam_digest = str(payload.get("iam_policy_sha256") or "")
        inventory_digest = str(payload.get("writer_inventory_sha256") or "")
        if not _ENVIRONMENT.fullmatch(environment_id):
            raise ValueError("trust environment invalid")
        if not _safe_bucket(formal_bucket):
            raise ValueError("trust formal bucket invalid")
        if not _SHA256.fullmatch(iam_digest) or not _SHA256.fullmatch(inventory_digest):
            raise ValueError("trust digest invalid")
        verified_at = _parse_time(payload.get("verified_at"))
        expires_at = _parse_time(payload.get("expires_at"))
        if (
            expires_at <= verified_at
            or expires_at - verified_at > MAX_TRUST_AGE
        ):
            raise ValueError("trust evidence lifetime invalid")
        return cls(
            environment_id=environment_id,
            formal_bucket=formal_bucket,
            iam_policy_sha256=iam_digest,
            writer_inventory_sha256=inventory_digest,
            verified_at=verified_at,
            expires_at=expires_at,
        )

    def evaluate(self, *, now, environment_id, formal_bucket):
        moment = _as_utc(now)
        allowed = bool(
            environment_id == self.environment_id
            and formal_bucket == self.formal_bucket
            and self.verified_at <= moment + FUTURE_SKEW
            and self.expires_at > moment
            and moment - self.verified_at <= MAX_TRUST_AGE
        )
        return NoOverwriteTrustDecision(
            allowed=allowed,
            error_code=None if allowed else "PURGE_NO_OVERWRITE_TRUST_INVALID",
            attestation_sha256=self.sha256,
        )

    @property
    def sha256(self):
        payload = {
            "schema_version": 1,
            "mode": TRUST_MODE,
            "result": "valid",
            "environment_id": self.environment_id,
            "formal_bucket": self.formal_bucket,
            "iam_policy_sha256": self.iam_policy_sha256,
            "writer_inventory_sha256": self.writer_inventory_sha256,
            "verified_at": self.verified_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_time(value):
    if not isinstance(value, str):
        raise ValueError("trust time invalid")
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise ValueError("trust time invalid") from exc


def _as_utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("trust time must include timezone")
    return value.astimezone(timezone.utc)


def _safe_bucket(value):
    return bool(
        isinstance(value, str)
        and value
        and "/" not in value
        and "\\" not in value
        and not any(ord(character) < 32 for character in value)
    )
