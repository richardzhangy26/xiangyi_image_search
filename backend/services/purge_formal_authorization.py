"""Canonical object manifest → formal-purge authorization bundle.

This module is the single parsing seam shared by verified-batch promotion and
later formal-delete revalidation.  It accepts the strict typed object manifest;
callers never reconstruct authorization dictionaries from JSON themselves.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from services.purge_object_backup import PurgeObjectBackupManifest


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PurgeFormalAuthorizationError(ValueError):
    error_code = "PURGE_FORMAL_AUTHORIZATION_INVALID"


@dataclass(frozen=True)
class FormalPurgeItemAuthorization:
    target_asset_id: uuid.UUID
    formal_bucket: str
    original_formal_key: str
    original_backup_object_id: str
    original_backup_sha256: str
    preview_formal_key: str
    preview_backup_object_id: str | None
    preview_backup_sha256: str | None
    preview_delete_authorized: bool
    authorization_retain_until: datetime


@dataclass(frozen=True)
class FormalPurgeAuthorizationBundle:
    purge_batch_id: uuid.UUID
    manifest_sha256: str
    database_backup_id: str
    database_manifest_sha256: str
    retain_until: datetime
    items: tuple[FormalPurgeItemAuthorization, ...]


def build_formal_purge_authorization_bundle(
    manifest: PurgeObjectBackupManifest,
    *,
    manifest_sha256: str,
    now: datetime,
) -> FormalPurgeAuthorizationBundle:
    """Return the complete immutable item authorization set or fail closed."""
    if not _SHA256.fullmatch(str(manifest_sha256)):
        raise PurgeFormalAuthorizationError("对象 manifest 摘要无效")
    moment = _as_utc(now)
    try:
        # Reparse through the strict public constructor so directly-created
        # dataclass instances cannot bypass the canonical manifest contract.
        canonical = PurgeObjectBackupManifest.from_dict(manifest.to_dict())
        batch_id = uuid.UUID(canonical.purge_batch_id)
        retain_until = _as_utc(
            datetime.fromisoformat(
                str(canonical.retention["retain_until"]).replace("Z", "+00:00")
            )
        )
        asset_ids = tuple(uuid.UUID(value) for value in canonical.asset_ids)
    except (KeyError, TypeError, ValueError) as exc:
        raise PurgeFormalAuthorizationError("对象 manifest 不能生成正式授权") from exc
    if retain_until <= moment:
        raise PurgeFormalAuthorizationError("对象 manifest 保留期已到期")

    copied_originals = {
        item.asset_ids[0]: item
        for item in canonical.objects
        if item.kind == "source_image"
    }
    copied_previews = tuple(
        item for item in canonical.objects if item.kind == "search_preview"
    )
    protected_previews = tuple(canonical.reference_protected)
    authorizations: list[FormalPurgeItemAuthorization] = []
    for asset_id in canonical.asset_ids:
        original = copied_originals.get(asset_id)
        copied = [item for item in copied_previews if asset_id in item.asset_ids]
        protected = [
            item for item in protected_previews
            if asset_id in item.selected_asset_ids
        ]
        if original is None or len(copied) + len(protected) != 1:
            raise PurgeFormalAuthorizationError("对象 manifest 的逐项成员不完整")
        preview = copied[0] if copied else protected[0]
        if preview.formal_bucket != original.formal_bucket:
            raise PurgeFormalAuthorizationError("正式 Bucket 身份不一致")
        preview_delete_authorized = bool(
            copied and asset_id == max(copied[0].asset_ids)
        )
        authorizations.append(FormalPurgeItemAuthorization(
            target_asset_id=uuid.UUID(asset_id),
            formal_bucket=original.formal_bucket,
            original_formal_key=original.formal_key,
            original_backup_object_id=original.object_id,
            original_backup_sha256=original.sha256,
            preview_formal_key=preview.formal_key,
            preview_backup_object_id=(copied[0].object_id if copied else None),
            preview_backup_sha256=(copied[0].sha256 if copied else None),
            preview_delete_authorized=preview_delete_authorized,
            authorization_retain_until=retain_until,
        ))
    if tuple(item.target_asset_id for item in authorizations) != asset_ids:
        raise PurgeFormalAuthorizationError("对象 manifest 资产顺序不一致")
    return FormalPurgeAuthorizationBundle(
        purge_batch_id=batch_id,
        manifest_sha256=str(manifest_sha256),
        database_backup_id=str(
            canonical.database_restore_point["backup_id"]
        ),
        database_manifest_sha256=str(
            canonical.database_restore_point["manifest_sha256"]
        ),
        retain_until=retain_until,
        items=tuple(authorizations),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise PurgeFormalAuthorizationError("正式授权时间必须包含时区")
    return value.astimezone(timezone.utc)
