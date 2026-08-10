import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.backup_storage import BackupObject, BackupStorageConflictError
from services.purge_object_backup import (
    PurgeObjectBackupItem,
    PurgeObjectBackupManifest,
)
from services.purge_object_restore import (
    PurgeObjectRestoreConfig,
    PurgeObjectRestoreConfigError,
    PurgeObjectRestoreConflictError,
    PurgeObjectRestoreIntegrityError,
    PurgeObjectRestoreService,
)


NOW = datetime(2026, 8, 9, 4, 0, tzinfo=timezone.utc)


class MemoryReadableStore:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def head(self, key):
        stored = self.objects.get(key)
        if stored is None:
            return None
        data, metadata = stored
        return BackupObject(key=key, size=len(data), metadata=dict(metadata))

    def download_to(self, key, target):
        target.write(self.objects[key][0])


class MemoryFileWriteOnceStore(MemoryReadableStore):
    def __init__(self):
        super().__init__()
        self.writes = []

    def put_file_if_absent(self, key, path, *, metadata):
        if key in self.objects:
            raise BackupStorageConflictError("fake conflict")
        self.objects[key] = (Path(path).read_bytes(), dict(metadata))
        self.writes.append(key)


def _canonical(payload):
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _manifest_and_backup():
    batch_id = "batch-001"
    backup_id = f"purge-{batch_id}"
    prefix = f"postgresql-backups/{backup_id}/objects"
    objects = []
    payloads = {
        "source_image": ("formal/a.png", b"source-a"),
        "search_preview": ("formal/p.jpg", b"preview-a"),
    }
    backup_objects = {}
    for kind, (formal_key, data) in payloads.items():
        object_id = hashlib.sha256(
            f"private-image-assets\0{formal_key}".encode()
        ).hexdigest()
        backup_key = f"{prefix}/payloads/{kind}/{object_id}"
        sha256 = hashlib.sha256(data).hexdigest()
        item = PurgeObjectBackupItem(
            object_id=object_id,
            kind=kind,
            asset_ids=("asset-a",),
            formal_bucket="private-image-assets",
            formal_key=formal_key,
            backup_bucket="private-backups",
            backup_key=backup_key,
            size=len(data),
            sha256=sha256,
            selected_reference_count=1,
            total_reference_count=1,
            remaining_reference_count=0,
            reference_set_sha256="a" * 64,
            verification={
                "source_head_download": "passed",
                "backup_head": "passed",
                "backup_download_sha256": "passed",
            },
        )
        objects.append(item)
        backup_objects[backup_key] = (
            data,
            {
                "purge-batch-id": batch_id,
                "object-id": object_id,
                "object-kind": kind,
                "sha256": sha256,
                "retention-days": "30",
            },
        )
    manifest = PurgeObjectBackupManifest(
        schema_version=1,
        status="complete",
        kind="purge_object_backup",
        purge_batch_id=batch_id,
        database_restore_point={
            "backup_id": backup_id,
            "purge_batch_id": batch_id,
            "remote_bucket": "private-backups",
            "remote_manifest_key": (
                f"postgresql-backups/{backup_id}/manifest.json"
            ),
            "manifest_sha256": "b" * 64,
            "artifact_sha256": "c" * 64,
            "completed_at": NOW.isoformat().replace("+00:00", "Z"),
            "retain_until": (
                NOW + timedelta(days=30)
            ).isoformat().replace("+00:00", "Z"),
        },
        asset_ids=("asset-a",),
        reference_catalog_version=1,
        reference_snapshot_sha256="d" * 64,
        plan_key=f"{prefix}/plan.json",
        manifest_key=f"{prefix}/manifest.json",
        objects=tuple(objects),
        reference_protected=(),
        retention={
            "days": 30,
            "retain_until": (
                NOW + timedelta(days=30)
            ).isoformat().replace("+00:00", "Z"),
        },
        created_at=NOW,
        completed_at=NOW,
        authorization="backup_only_no_delete",
        production_gates={
            "source_read_only_credential": "not_verified",
            "backup_private_sse": "not_verified",
            "backup_credential_no_delete": "not_verified",
            "retention_policy_30_days": "not_verified",
            "isolated_restore_drill": "not_verified",
        },
    )
    manifest_bytes = _canonical(manifest.to_dict())
    backup_objects[manifest.manifest_key] = (
        manifest_bytes,
        {
            "purge-batch-id": batch_id,
            "kind": "purge-object-manifest",
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        },
    )
    return manifest, MemoryReadableStore(backup_objects), payloads


def test_restore_uses_program_derived_isolated_keys_and_rechecks_bytes(tmp_path):
    manifest, backup, payloads = _manifest_and_backup()
    isolated = MemoryFileWriteOnceStore()
    service = PurgeObjectRestoreService(
        backup_store=backup,
        isolated_store=isolated,
        config=PurgeObjectRestoreConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            isolated_bucket="disposable-restore",
            isolated_prefix="isolated-restores",
            isolated_environment=True,
            temporary_root=tmp_path,
        ),
    )

    result = service.restore_to_isolation(
        manifest,
        restore_run_id="drill-001",
        acknowledge_isolated=True,
    )

    assert result.status == "verified"
    assert len(result.objects) == len(payloads)
    assert all(
        item.isolated_key.startswith(
            "isolated-restores/drill-001/purge-batch-001/objects/"
        )
        for item in result.objects
    )
    assert all(item.isolated_key != item.formal_key for item in result.objects)
    assert all(item.verification == "passed" for item in result.objects)


@pytest.mark.parametrize(
    ("acknowledge", "isolated_environment", "error_code"),
    [
        (False, True, "isolation_ack_required"),
        (True, False, "target_not_isolated"),
    ],
)
def test_restore_requires_acknowledgement_and_isolated_attestation(
    tmp_path,
    acknowledge,
    isolated_environment,
    error_code,
):
    manifest, backup, _ = _manifest_and_backup()
    service = PurgeObjectRestoreService(
        backup_store=backup,
        isolated_store=MemoryFileWriteOnceStore(),
        config=PurgeObjectRestoreConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            isolated_bucket="disposable-restore",
            isolated_prefix="isolated-restores",
            isolated_environment=isolated_environment,
            temporary_root=tmp_path,
        ),
    )

    with pytest.raises(PurgeObjectRestoreConfigError) as caught:
        service.restore_to_isolation(
            manifest,
            restore_run_id="drill-001",
            acknowledge_isolated=acknowledge,
        )

    assert caught.value.error_code == error_code


def test_identical_isolated_restore_is_idempotent_without_overwrite(tmp_path):
    manifest, backup, _ = _manifest_and_backup()
    isolated = MemoryFileWriteOnceStore()
    service = PurgeObjectRestoreService(
        backup_store=backup,
        isolated_store=isolated,
        config=PurgeObjectRestoreConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            isolated_bucket="disposable-restore",
            isolated_prefix="isolated-restores",
            isolated_environment=True,
            temporary_root=tmp_path,
        ),
    )

    first = service.restore_to_isolation(
        manifest,
        restore_run_id="drill-001",
        acknowledge_isolated=True,
    )
    writes = tuple(isolated.writes)
    second = service.restore_to_isolation(
        manifest,
        restore_run_id="drill-001",
        acknowledge_isolated=True,
    )

    assert second.to_dict() == first.to_dict()
    assert tuple(isolated.writes) == writes


def test_existing_mismatched_isolated_object_is_a_conflict_not_an_overwrite(
    tmp_path,
):
    manifest, backup, _ = _manifest_and_backup()
    isolated = MemoryFileWriteOnceStore()
    service = PurgeObjectRestoreService(
        backup_store=backup,
        isolated_store=isolated,
        config=PurgeObjectRestoreConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            isolated_bucket="disposable-restore",
            isolated_prefix="isolated-restores",
            isolated_environment=True,
            temporary_root=tmp_path,
        ),
    )
    first = service.restore_to_isolation(
        manifest,
        restore_run_id="drill-001",
        acknowledge_isolated=True,
    )
    target_key = first.objects[0].isolated_key
    _, metadata = isolated.objects[target_key]
    isolated.objects[target_key] = (b"X" * first.objects[0].size, metadata)
    writes = tuple(isolated.writes)

    with pytest.raises(PurgeObjectRestoreConflictError) as caught:
        service.restore_to_isolation(
            manifest,
            restore_run_id="drill-001",
            acknowledge_isolated=True,
        )

    assert caught.value.error_code == "isolated_restore_conflict"
    assert tuple(isolated.writes) == writes


def test_corrupt_backup_payload_fails_before_any_isolated_write(tmp_path):
    manifest, backup, _ = _manifest_and_backup()
    payload_key = manifest.objects[0].backup_key
    _, metadata = backup.objects[payload_key]
    backup.objects[payload_key] = (b"X" * manifest.objects[0].size, metadata)
    isolated = MemoryFileWriteOnceStore()
    service = PurgeObjectRestoreService(
        backup_store=backup,
        isolated_store=isolated,
        config=PurgeObjectRestoreConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            isolated_bucket="disposable-restore",
            isolated_prefix="isolated-restores",
            isolated_environment=True,
            temporary_root=tmp_path,
        ),
    )

    with pytest.raises(PurgeObjectRestoreIntegrityError):
        service.restore_to_isolation(
            manifest,
            restore_run_id="drill-001",
            acknowledge_isolated=True,
        )

    assert isolated.objects == {}
