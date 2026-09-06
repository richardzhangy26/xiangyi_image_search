import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from services.purge_object_backup import (
    PurgeObjectBackupItem,
    PurgeObjectBackupManifest,
)


def _object_id(bucket: str, key: str) -> str:
    return hashlib.sha256((bucket + "\0" + key).encode("utf-8")).hexdigest()


def _backup_item(*, batch_id: str, asset_ids: tuple[str, ...], kind: str, key: str, digest: str):
    bucket = "formal-test-bucket"
    object_id = _object_id(bucket, key)
    return PurgeObjectBackupItem(
        object_id=object_id,
        kind=kind,
        asset_ids=asset_ids,
        formal_bucket=bucket,
        formal_key=key,
        backup_bucket="backup-test-bucket",
        backup_key=f"backups/purge-{batch_id}/objects/payloads/{kind}/{object_id}",
        size=17,
        sha256=digest,
        selected_reference_count=len(asset_ids),
        total_reference_count=len(asset_ids),
        remaining_reference_count=0,
        reference_set_sha256="d" * 64,
        verification={
            "source_head_download": "passed",
            "backup_head": "passed",
            "backup_download_sha256": "passed",
        },
    )


def complete_manifest(*, batch_id: str | None = None, asset_ids: tuple[str, ...] | None = None):
    resolved_batch_id = batch_id or str(uuid.uuid4())
    resolved_asset_ids = tuple(sorted(asset_ids or (str(uuid.uuid4()),)))
    created_at = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
    retain_until = created_at + timedelta(days=30)
    created_text = created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    retain_text = retain_until.strftime("%Y-%m-%dT%H:%M:%SZ")
    originals = tuple(
        _backup_item(
            batch_id=resolved_batch_id,
            asset_ids=(asset_id,),
            kind="source_image",
            key=f"original/{asset_id}",
            digest="a" * 64,
        )
        for asset_id in resolved_asset_ids
    )
    preview = _backup_item(
        batch_id=resolved_batch_id,
        asset_ids=resolved_asset_ids,
        kind="search_preview",
        key="preview/shared",
        digest="b" * 64,
    )
    return PurgeObjectBackupManifest(
        schema_version=1,
        status="complete",
        kind="purge_object_backup",
        purge_batch_id=resolved_batch_id,
        database_restore_point={
            "backup_id": f"purge-{resolved_batch_id}",
            "purge_batch_id": resolved_batch_id,
            "remote_bucket": "backup-test-bucket",
            "remote_manifest_key": f"backups/purge-{resolved_batch_id}/manifest.json",
            "manifest_sha256": "c" * 64,
            "artifact_sha256": "e" * 64,
            "completed_at": created_text,
            "retain_until": retain_text,
        },
        asset_ids=resolved_asset_ids,
        reference_catalog_version=1,
        reference_snapshot_sha256="f" * 64,
        plan_key=f"backups/purge-{resolved_batch_id}/objects/plan.json",
        manifest_key=f"backups/purge-{resolved_batch_id}/objects/manifest.json",
        objects=originals + (preview,),
        reference_protected=(),
        retention={"days": 30, "retain_until": retain_text},
        created_at=created_at,
        completed_at=created_at + timedelta(minutes=1),
        authorization="backup_only_no_delete",
        production_gates={
            "source_read_only_credential": "not_verified",
            "backup_private_sse": "not_verified",
            "backup_credential_no_delete": "not_verified",
            "retention_policy_30_days": "not_verified",
            "isolated_restore_drill": "not_verified",
        },
    )
