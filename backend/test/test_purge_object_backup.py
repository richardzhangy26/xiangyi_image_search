import hashlib
import io
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import services.purge_object_backup as purge_module
from services.backup_storage import (
    BackupObject,
    BackupStorageConflictError,
    BackupStorageError,
)
from services.postgres_backup import BackupManifest
from services.purge_object_backup import (
    CompleteReferenceSnapshot,
    ObjectReference,
    PurgeAssetSnapshot,
    PurgeObjectBackupConfig,
    PurgeObjectBackupManifest,
    PurgeObjectBackupRequest,
    PurgeObjectBackupService,
    PurgeObjectIntegrityError,
    PurgeObjectReferenceError,
    ReferenceSourceSlice,
)


NOW = datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc)


class FakeRestorePointGate:
    def __init__(self, manifest):
        self.manifest = manifest

    def require_verified(self, purge_batch_id):
        return self.manifest


class FakeReferenceSnapshotReader:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def capture_for_purge(self, asset_ids):
        return self.snapshot


class SequenceReferenceSnapshotReader:
    def __init__(self, *snapshots):
        self.snapshots = list(snapshots)

    def capture_for_purge(self, asset_ids):
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]


class MemoryObjectReader:
    def __init__(self, objects):
        self.objects = dict(objects)

    def head(self, key):
        stored = self.objects.get(key)
        if stored is None:
            return None
        data, metadata = stored
        return BackupObject(key=key, size=len(data), metadata=dict(metadata))

    def download_to(self, key, target):
        target.write(self.objects[key][0])


class MemoryWriteOnceStore(MemoryObjectReader):
    def __init__(self):
        super().__init__({})
        self.writes = []

    def put_file_if_absent(self, key, path, *, metadata):
        if key in self.objects:
            raise BackupStorageConflictError("fake conflict")
        self.objects[key] = (Path(path).read_bytes(), dict(metadata))
        self.writes.append(key)

    def put_bytes_if_absent(self, key, data, *, metadata):
        if key in self.objects:
            raise BackupStorageConflictError("fake conflict")
        self.objects[key] = (bytes(data), dict(metadata))
        self.writes.append(key)


class FailFinalManifestStore(MemoryWriteOnceStore):
    def put_bytes_if_absent(self, key, data, *, metadata):
        if key.endswith("/objects/manifest.json"):
            raise BackupStorageError("fake final failure")
        super().put_bytes_if_absent(key, data, metadata=metadata)


class FailOncePreviewPayloadStore(MemoryWriteOnceStore):
    def __init__(self):
        super().__init__()
        self.failed = False

    def put_file_if_absent(self, key, path, *, metadata):
        if "/payloads/search_preview/" in key and not self.failed:
            self.failed = True
            raise BackupStorageError("fake interrupted payload")
        super().put_file_if_absent(key, path, metadata=metadata)


def _restore_point_manifest(batch_id="batch-001"):
    backup_id = f"purge-{batch_id}"
    return BackupManifest(
        schema_version=1,
        status="complete",
        backup_id=backup_id,
        kind="purge_restore_point",
        purge_batch_id=batch_id,
        created_at=NOW,
        completed_at=NOW + timedelta(minutes=1),
        database_identity={
            "host": "source-db.invalid",
            "port": 5432,
            "database": "image_search",
            "system_identifier": "123456",
        },
        postgres_client_major=16,
        postgres_server_major=16,
        artifact_file="backup.dump",
        artifact_size=128,
        artifact_sha256="d" * 64,
        local_relative_path=f"{backup_id}/backup.dump",
        remote_bucket="private-backups",
        remote_dump_key=f"postgresql-backups/{backup_id}/backup.dump",
        remote_manifest_key=f"postgresql-backups/{backup_id}/manifest.json",
        retention_days=30,
        retain_until=NOW + timedelta(days=30),
        verification={
            "local_pg_restore_list": "passed",
            "remote_dump_sha256": "passed",
            "remote_manifest_readback": "passed",
        },
        production_gates={
            "bucket_private": "not_verified",
            "server_side_encryption": "not_verified",
            "credential_least_privilege_no_delete": "not_verified",
            "retention_policy_30_days": "not_verified",
            "remote_restore_drill": "not_verified",
        },
    )


def _snapshot(original_data, preview_data):
    token = "repeatable-read-snapshot-1"
    return CompleteReferenceSnapshot(
        catalog_version=1,
        consistency_token=token,
        captured_at=NOW,
        targets=(
            PurgeAssetSnapshot(
                asset_id="asset-a",
                status="archived",
                original_key="formal/a.png",
                preview_key="formal/p.jpg",
                original_size=len(original_data),
                original_sha256=hashlib.sha256(original_data).hexdigest(),
                normalization_version="preview-v1",
            ),
        ),
        source_slices=(
            ReferenceSourceSlice(
                source="image_assets",
                consistency_token=token,
                status="complete",
                truncated=False,
                enumerated_count=2,
            ),
            ReferenceSourceSlice(
                source="image_import_items",
                consistency_token=token,
                status="complete",
                truncated=False,
                enumerated_count=0,
            ),
        ),
        references=(
            ObjectReference(
                source="image_assets",
                owner_id="asset-a",
                owner_state="archived",
                kind="source_image",
                formal_key="formal/a.png",
            ),
            ObjectReference(
                source="image_assets",
                owner_id="asset-a",
                owner_state="archived",
                kind="search_preview",
                formal_key="formal/p.jpg",
            ),
        ),
    )


def test_create_verified_binds_restore_point_and_commits_source_and_preview(tmp_path):
    original = b"source-a"
    preview = b"preview-a"
    formal = MemoryObjectReader(
        {
            "formal/a.png": (
                original,
                {
                    "sha256": hashlib.sha256(original).hexdigest(),
                    "source-size": str(len(original)),
                },
            ),
            "formal/p.jpg": (
                preview,
                {
                    "preview-size": str(len(preview)),
                    "preview-md5": hashlib.md5(preview).hexdigest(),
                },
            ),
        }
    )
    backup = MemoryWriteOnceStore()
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=FakeReferenceSnapshotReader(_snapshot(original, preview)),
        formal_objects=formal,
        backup_store=backup,
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path / "purge-object-manifests",
        ),
        now=lambda: NOW,
    )

    result = service.create_verified(
        PurgeObjectBackupRequest(
            purge_batch_id="batch-001",
            asset_ids=("asset-a",),
        )
    )

    assert result.status == "complete"
    assert result.manifest.database_restore_point["backup_id"] == "purge-batch-001"
    assert result.manifest.database_restore_point["remote_bucket"] == "private-backups"
    assert [(item.kind, item.asset_ids) for item in result.manifest.objects] == [
        ("source_image", ("asset-a",)),
        ("search_preview", ("asset-a",)),
    ]
    assert result.manifest.objects[1].sha256 == hashlib.sha256(preview).hexdigest()
    assert result.manifest.retention["days"] == 30
    assert result.manifest.to_dict()["authorization"] == "backup_only_no_delete"
    assert result.manifest_key in backup.objects
    assert backup.writes[0].endswith("/objects/plan.json")
    assert backup.writes[-1] == result.manifest_key


def test_shared_last_reference_preview_is_backed_up_once_with_reference_evidence(
    tmp_path,
):
    source_a = b"source-a"
    source_b = b"source-b"
    preview = b"shared-preview"
    token = "repeatable-read-snapshot-shared"
    targets = (
        PurgeAssetSnapshot(
            asset_id="asset-a",
            status="archived",
            original_key="formal/a.png",
            preview_key="formal/shared.jpg",
            original_size=len(source_a),
            original_sha256=hashlib.sha256(source_a).hexdigest(),
            normalization_version="preview-v1",
        ),
        PurgeAssetSnapshot(
            asset_id="asset-b",
            status="archived",
            original_key="formal/b.png",
            preview_key="formal/shared.jpg",
            original_size=len(source_b),
            original_sha256=hashlib.sha256(source_b).hexdigest(),
            normalization_version="preview-v1",
        ),
    )
    references = (
        ObjectReference(
            "image_assets", "asset-a", "archived", "source_image", "formal/a.png"
        ),
        ObjectReference(
            "image_assets", "asset-b", "archived", "source_image", "formal/b.png"
        ),
        ObjectReference(
            "image_assets",
            "asset-a",
            "archived",
            "search_preview",
            "formal/shared.jpg",
        ),
        ObjectReference(
            "image_assets",
            "asset-b",
            "archived",
            "search_preview",
            "formal/shared.jpg",
        ),
    )
    snapshot = CompleteReferenceSnapshot(
        catalog_version=1,
        consistency_token=token,
        captured_at=NOW,
        targets=targets,
        source_slices=(
            ReferenceSourceSlice(
                "image_assets", token, "complete", False, len(references)
            ),
            ReferenceSourceSlice(
                "image_import_items", token, "complete", False, 0
            ),
        ),
        references=references,
    )
    formal = MemoryObjectReader(
        {
            "formal/a.png": (
                source_a,
                {
                    "sha256": hashlib.sha256(source_a).hexdigest(),
                    "source-size": str(len(source_a)),
                },
            ),
            "formal/b.png": (
                source_b,
                {
                    "sha256": hashlib.sha256(source_b).hexdigest(),
                    "source-size": str(len(source_b)),
                },
            ),
            "formal/shared.jpg": (
                preview,
                {
                    "preview-size": str(len(preview)),
                    "preview-md5": hashlib.md5(preview).hexdigest(),
                },
            ),
        }
    )
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=FakeReferenceSnapshotReader(snapshot),
        formal_objects=formal,
        backup_store=MemoryWriteOnceStore(),
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path / "purge-object-manifests",
        ),
        now=lambda: NOW,
    )

    result = service.create_verified(
        PurgeObjectBackupRequest("batch-001", ("asset-b", "asset-a"))
    )

    previews = [
        item for item in result.manifest.objects if item.kind == "search_preview"
    ]
    assert len(previews) == 1
    assert previews[0].asset_ids == ("asset-a", "asset-b")
    assert previews[0].selected_reference_count == 2
    assert previews[0].total_reference_count == 2
    assert previews[0].remaining_reference_count == 0
    assert len(previews[0].reference_set_sha256) == 64


def test_unfinished_import_reference_protects_shared_preview_with_evidence(tmp_path):
    original = b"source-a"
    token = "repeatable-read-snapshot-import"
    snapshot = CompleteReferenceSnapshot(
        catalog_version=1,
        consistency_token=token,
        captured_at=NOW,
        targets=(
            PurgeAssetSnapshot(
                asset_id="asset-a",
                status="archived",
                original_key="formal/a.png",
                preview_key="formal/shared.jpg",
                original_size=len(original),
                original_sha256=hashlib.sha256(original).hexdigest(),
                normalization_version="preview-v1",
            ),
        ),
        source_slices=(
            ReferenceSourceSlice("image_assets", token, "complete", False, 2),
            ReferenceSourceSlice(
                "image_import_items", token, "complete", False, 1
            ),
        ),
        references=(
            ObjectReference(
                "image_assets",
                "asset-a",
                "archived",
                "source_image",
                "formal/a.png",
            ),
            ObjectReference(
                "image_assets",
                "asset-a",
                "archived",
                "search_preview",
                "formal/shared.jpg",
            ),
            ObjectReference(
                "image_import_items",
                "import-1",
                "unfinished",
                "search_preview",
                "formal/shared.jpg",
            ),
        ),
    )
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=FakeReferenceSnapshotReader(snapshot),
        formal_objects=MemoryObjectReader(
            {
                "formal/a.png": (
                    original,
                    {
                        "sha256": hashlib.sha256(original).hexdigest(),
                        "source-size": str(len(original)),
                    },
                )
            }
        ),
        backup_store=MemoryWriteOnceStore(),
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path / "purge-object-manifests",
        ),
        now=lambda: NOW,
    )

    result = service.create_verified(PurgeObjectBackupRequest("batch-001", ("asset-a",)))

    assert [item.kind for item in result.manifest.objects] == ["source_image"]
    assert len(result.manifest.reference_protected) == 1
    protected = result.manifest.reference_protected[0]
    assert protected.selected_reference_count == 1
    assert protected.total_reference_count == 2
    assert protected.remaining_reference_count == 1
    assert len(protected.reference_set_sha256) == 64


def test_duplicate_reference_edge_fails_before_any_backup_write(tmp_path):
    original = b"source-a"
    preview = b"preview-a"
    base = _snapshot(original, preview)
    duplicate = base.references[-1]
    snapshot = replace(
        base,
        source_slices=(
            replace(base.source_slices[0], enumerated_count=3),
            base.source_slices[1],
        ),
        references=base.references + (duplicate,),
    )
    backup = MemoryWriteOnceStore()
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=FakeReferenceSnapshotReader(snapshot),
        formal_objects=MemoryObjectReader({}),
        backup_store=backup,
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path / "purge-object-manifests",
        ),
        now=lambda: NOW,
    )

    with pytest.raises(PurgeObjectReferenceError) as caught:
        service.create_verified(PurgeObjectBackupRequest("batch-001", ("asset-a",)))

    assert caught.value.error_code == "duplicate_reference"
    assert backup.objects == {}


def test_identical_retry_reconciles_existing_final_manifest_without_new_writes(
    tmp_path,
):
    original = b"source-a"
    preview = b"preview-a"
    formal = MemoryObjectReader(
        {
            "formal/a.png": (
                original,
                {
                    "sha256": hashlib.sha256(original).hexdigest(),
                    "source-size": str(len(original)),
                },
            ),
            "formal/p.jpg": (
                preview,
                {
                    "preview-size": str(len(preview)),
                    "preview-md5": hashlib.md5(preview).hexdigest(),
                },
            ),
        }
    )
    backup = MemoryWriteOnceStore()
    current_time = [NOW]
    references = FakeReferenceSnapshotReader(_snapshot(original, preview))
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=references,
        formal_objects=formal,
        backup_store=backup,
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path / "purge-object-manifests",
        ),
        now=lambda: current_time[0],
    )

    first = service.create_verified(PurgeObjectBackupRequest("batch-001", ("asset-a",)))
    writes_after_first = tuple(backup.writes)
    current_time[0] = NOW + timedelta(hours=1)
    references.snapshot = replace(
        references.snapshot,
        captured_at=current_time[0],
    )

    second = service.create_verified(PurgeObjectBackupRequest("batch-001", ("asset-a",)))

    assert second.manifest_sha256 == first.manifest_sha256
    assert second.manifest.to_dict() == first.manifest.to_dict()
    assert tuple(backup.writes) == writes_after_first


def test_remote_final_failure_does_not_leave_local_complete_manifest(tmp_path):
    original = b"source-a"
    preview = b"preview-a"
    local_root = tmp_path / "purge-object-manifests"
    backup = FailFinalManifestStore()
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=FakeReferenceSnapshotReader(_snapshot(original, preview)),
        formal_objects=MemoryObjectReader(
            {
                "formal/a.png": (
                    original,
                    {
                        "sha256": hashlib.sha256(original).hexdigest(),
                        "source-size": str(len(original)),
                    },
                ),
                "formal/p.jpg": (
                    preview,
                    {
                        "preview-size": str(len(preview)),
                        "preview-md5": hashlib.md5(preview).hexdigest(),
                    },
                ),
            }
        ),
        backup_store=backup,
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=local_root,
        ),
        now=lambda: NOW,
    )

    with pytest.raises(Exception) as caught:
        service.create_verified(PurgeObjectBackupRequest("batch-001", ("asset-a",)))

    assert getattr(caught.value, "stage", None) == "backup_put"
    assert not (
        local_root / "purge-batch-001" / "objects" / "manifest.json"
    ).exists()
    assert not any(key.endswith("/objects/manifest.json") for key in backup.objects)


def test_current_revalidation_only_shrinks_to_still_unreferenced_backed_objects(
    tmp_path,
):
    original = b"source-a"
    preview = b"preview-a"
    references = FakeReferenceSnapshotReader(_snapshot(original, preview))
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=references,
        formal_objects=MemoryObjectReader(
            {
                "formal/a.png": (
                    original,
                    {
                        "sha256": hashlib.sha256(original).hexdigest(),
                        "source-size": str(len(original)),
                    },
                ),
                "formal/p.jpg": (
                    preview,
                    {
                        "preview-size": str(len(preview)),
                        "preview-md5": hashlib.md5(preview).hexdigest(),
                    },
                ),
            }
        ),
        backup_store=MemoryWriteOnceStore(),
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path / "purge-object-manifests",
        ),
        now=lambda: NOW,
    )
    result = service.create_verified(
        PurgeObjectBackupRequest("batch-001", ("asset-a",))
    )
    base = references.snapshot
    references.snapshot = replace(
        base,
        source_slices=(
            replace(base.source_slices[0], enumerated_count=3),
            base.source_slices[1],
        ),
        references=base.references
        + (
            ObjectReference(
                "image_assets",
                "asset-live",
                "active",
                "search_preview",
                "formal/p.jpg",
            ),
        ),
    )

    current = service.revalidate_current_candidates(result.manifest)

    assert current.status == "current"
    assert [item.kind for item in current.objects] == ["source_image"]
    assert current.reference_snapshot_sha256 != (
        result.manifest.reference_snapshot_sha256
    )


def test_manifest_parser_rejects_boolean_schema_version_instead_of_coercing_it(
    tmp_path,
):
    original = b"source-a"
    preview = b"preview-a"
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=FakeReferenceSnapshotReader(_snapshot(original, preview)),
        formal_objects=MemoryObjectReader(
            {
                "formal/a.png": (
                    original,
                    {
                        "sha256": hashlib.sha256(original).hexdigest(),
                        "source-size": str(len(original)),
                    },
                ),
                "formal/p.jpg": (
                    preview,
                    {
                        "preview-size": str(len(preview)),
                        "preview-md5": hashlib.md5(preview).hexdigest(),
                    },
                ),
            }
        ),
        backup_store=MemoryWriteOnceStore(),
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path / "purge-object-manifests",
        ),
        now=lambda: NOW,
    )
    payload = service.create_verified(
        PurgeObjectBackupRequest("batch-001", ("asset-a",))
    ).manifest.to_dict()
    payload["schema_version"] = True

    with pytest.raises(PurgeObjectIntegrityError) as caught:
        PurgeObjectBackupManifest.from_dict(payload)

    assert caught.value.error_code == "invalid_manifest"


def test_partial_retry_reconciles_the_same_immutable_plan_at_a_later_time(
    tmp_path,
):
    original = b"source-a"
    preview = b"preview-a"
    current_time = [NOW]
    backup = FailOncePreviewPayloadStore()
    references = FakeReferenceSnapshotReader(_snapshot(original, preview))
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=references,
        formal_objects=MemoryObjectReader(
            {
                "formal/a.png": (
                    original,
                    {
                        "sha256": hashlib.sha256(original).hexdigest(),
                        "source-size": str(len(original)),
                    },
                ),
                "formal/p.jpg": (
                    preview,
                    {
                        "preview-size": str(len(preview)),
                        "preview-md5": hashlib.md5(preview).hexdigest(),
                    },
                ),
            }
        ),
        backup_store=backup,
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path / "purge-object-manifests",
        ),
        now=lambda: current_time[0],
    )

    with pytest.raises(PurgeObjectIntegrityError):
        service.create_verified(PurgeObjectBackupRequest("batch-001", ("asset-a",)))
    current_time[0] = NOW + timedelta(hours=1)
    references.snapshot = replace(
        references.snapshot,
        captured_at=current_time[0],
    )

    result = service.create_verified(
        PurgeObjectBackupRequest("batch-001", ("asset-a",))
    )

    assert result.status == "complete"
    assert sum(key.endswith("/objects/plan.json") for key in backup.writes) == 1
    assert backup.writes[-1] == result.manifest_key


def test_unknown_reference_owner_state_fails_closed_before_storage_access(
    tmp_path,
):
    original = b"source-a"
    preview = b"preview-a"
    base = _snapshot(original, preview)
    snapshot = replace(
        base,
        references=(
            base.references[0],
            replace(base.references[1], owner_state="deleted"),
        ),
    )
    backup = MemoryWriteOnceStore()
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=FakeReferenceSnapshotReader(snapshot),
        formal_objects=MemoryObjectReader({}),
        backup_store=backup,
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path,
        ),
        now=lambda: NOW,
    )

    with pytest.raises(PurgeObjectReferenceError) as caught:
        service.create_verified(PurgeObjectBackupRequest("batch-001", ("asset-a",)))

    assert caught.value.error_code == "invalid_reference_state"
    assert backup.objects == {}


@pytest.mark.parametrize(
    ("case", "error_code"),
    [
        ("missing_import_slice", "reference_sources_incomplete"),
        ("mismatched_token", "reference_slice_incomplete"),
        ("truncated", "reference_slice_incomplete"),
        ("wrong_count", "reference_slice_incomplete"),
        ("non_archived", "asset_not_archived"),
        ("missing_preview_edge", "target_preview_reference_missing"),
        ("mixed_role", "mixed_object_role"),
        ("shared_original", "source_image_not_exclusive"),
    ],
)
def test_incomplete_or_inconsistent_reference_snapshot_fails_closed(
    tmp_path,
    case,
    error_code,
):
    original = b"source-a"
    preview = b"preview-a"
    base = _snapshot(original, preview)
    if case == "missing_import_slice":
        snapshot = replace(base, source_slices=(base.source_slices[0],))
    elif case == "mismatched_token":
        snapshot = replace(
            base,
            source_slices=(
                base.source_slices[0],
                replace(base.source_slices[1], consistency_token="other"),
            ),
        )
    elif case == "truncated":
        snapshot = replace(
            base,
            source_slices=(
                replace(base.source_slices[0], truncated=True),
                base.source_slices[1],
            ),
        )
    elif case == "wrong_count":
        snapshot = replace(
            base,
            source_slices=(
                replace(base.source_slices[0], enumerated_count=99),
                base.source_slices[1],
            ),
        )
    elif case == "non_archived":
        snapshot = replace(
            base,
            targets=(replace(base.targets[0], status="active"),),
        )
    elif case == "missing_preview_edge":
        snapshot = replace(
            base,
            source_slices=(
                replace(base.source_slices[0], enumerated_count=1),
                base.source_slices[1],
            ),
            references=(base.references[0],),
        )
    elif case == "mixed_role":
        snapshot = replace(
            base,
            source_slices=(
                replace(base.source_slices[0], enumerated_count=3),
                base.source_slices[1],
            ),
            references=base.references
            + (
                ObjectReference(
                    "image_assets",
                    "asset-live",
                    "active",
                    "source_image",
                    "formal/p.jpg",
                ),
            ),
        )
    else:
        snapshot = replace(
            base,
            source_slices=(
                replace(base.source_slices[0], enumerated_count=3),
                base.source_slices[1],
            ),
            references=base.references
            + (
                ObjectReference(
                    "image_assets",
                    "asset-live",
                    "active",
                    "source_image",
                    "formal/a.png",
                ),
            ),
        )
    backup = MemoryWriteOnceStore()
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=FakeReferenceSnapshotReader(snapshot),
        formal_objects=MemoryObjectReader({}),
        backup_store=backup,
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path,
        ),
        now=lambda: NOW,
    )

    with pytest.raises(PurgeObjectReferenceError) as caught:
        service.create_verified(PurgeObjectBackupRequest("batch-001", ("asset-a",)))

    assert caught.value.error_code == error_code
    assert backup.objects == {}


def test_reference_change_during_copy_prevents_final_manifest(tmp_path):
    original = b"source-a"
    preview = b"preview-a"
    initial = _snapshot(original, preview)
    changed = replace(
        initial,
        source_slices=(
            replace(initial.source_slices[0], enumerated_count=3),
            initial.source_slices[1],
        ),
        references=initial.references
        + (
            ObjectReference(
                "image_assets",
                "asset-live",
                "active",
                "search_preview",
                "formal/p.jpg",
            ),
        ),
    )
    backup = MemoryWriteOnceStore()
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=SequenceReferenceSnapshotReader(initial, changed),
        formal_objects=MemoryObjectReader(
            {
                "formal/a.png": (
                    original,
                    {
                        "sha256": hashlib.sha256(original).hexdigest(),
                        "source-size": str(len(original)),
                    },
                ),
                "formal/p.jpg": (
                    preview,
                    {
                        "preview-size": str(len(preview)),
                        "preview-md5": hashlib.md5(preview).hexdigest(),
                    },
                ),
            }
        ),
        backup_store=backup,
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path,
        ),
        now=lambda: NOW,
    )

    with pytest.raises(PurgeObjectReferenceError) as caught:
        service.create_verified(PurgeObjectBackupRequest("batch-001", ("asset-a",)))

    assert caught.value.error_code == "reference_snapshot_changed"
    assert not any(key.endswith("/objects/manifest.json") for key in backup.objects)


def test_revalidation_rejects_new_last_reference_that_was_not_backed_up(
    tmp_path,
):
    original = b"source-a"
    preview = b"preview-a"
    base = _snapshot(original, preview)
    initially_protected = replace(
        base,
        source_slices=(
            base.source_slices[0],
            replace(base.source_slices[1], enumerated_count=1),
        ),
        references=base.references
        + (
            ObjectReference(
                "image_import_items",
                "import-1",
                "unfinished",
                "search_preview",
                "formal/p.jpg",
            ),
        ),
    )
    references = FakeReferenceSnapshotReader(initially_protected)
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=references,
        formal_objects=MemoryObjectReader(
            {
                "formal/a.png": (
                    original,
                    {
                        "sha256": hashlib.sha256(original).hexdigest(),
                        "source-size": str(len(original)),
                    },
                )
            }
        ),
        backup_store=MemoryWriteOnceStore(),
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path,
        ),
        now=lambda: NOW,
    )
    result = service.create_verified(
        PurgeObjectBackupRequest("batch-001", ("asset-a",))
    )
    references.snapshot = base

    with pytest.raises(PurgeObjectReferenceError) as caught:
        service.revalidate_current_candidates(result.manifest)

    assert caught.value.error_code == "unbacked_current_candidate"


def test_existing_same_size_different_backup_payload_is_never_overwritten(
    tmp_path,
):
    original = b"source-a"
    preview = b"preview-a"
    object_id = hashlib.sha256(
        b"private-image-assets\0formal/a.png"
    ).hexdigest()
    key = (
        "postgresql-backups/purge-batch-001/objects/"
        f"payloads/source_image/{object_id}"
    )
    expected_sha256 = hashlib.sha256(original).hexdigest()
    backup = MemoryWriteOnceStore()
    conflicting_bytes = b"X" * len(original)
    backup.objects[key] = (
        conflicting_bytes,
        {
            "purge-batch-id": "batch-001",
            "object-id": object_id,
            "object-kind": "source_image",
            "sha256": expected_sha256,
            "retention-days": "30",
        },
    )
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=FakeReferenceSnapshotReader(_snapshot(original, preview)),
        formal_objects=MemoryObjectReader(
            {
                "formal/a.png": (
                    original,
                    {
                        "sha256": expected_sha256,
                        "source-size": str(len(original)),
                    },
                ),
                "formal/p.jpg": (
                    preview,
                    {
                        "preview-size": str(len(preview)),
                        "preview-md5": hashlib.md5(preview).hexdigest(),
                    },
                ),
            }
        ),
        backup_store=backup,
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path,
        ),
        now=lambda: NOW,
    )

    with pytest.raises(PurgeObjectIntegrityError) as caught:
        service.create_verified(PurgeObjectBackupRequest("batch-001", ("asset-a",)))

    assert caught.value.error_code == "backup_object_conflict"
    assert backup.objects[key][0] == conflicting_bytes
    assert not any(value.endswith("/objects/manifest.json") for value in backup.objects)


@pytest.mark.parametrize(
    "mutation",
    ["unknown_field", "verified_gate", "unsafe_backup_key", "duplicate_object"],
)
def test_manifest_parser_rejects_noncanonical_or_self_asserted_contracts(
    tmp_path,
    mutation,
):
    original = b"source-a"
    preview = b"preview-a"
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=FakeReferenceSnapshotReader(_snapshot(original, preview)),
        formal_objects=MemoryObjectReader(
            {
                "formal/a.png": (
                    original,
                    {
                        "sha256": hashlib.sha256(original).hexdigest(),
                        "source-size": str(len(original)),
                    },
                ),
                "formal/p.jpg": (
                    preview,
                    {
                        "preview-size": str(len(preview)),
                        "preview-md5": hashlib.md5(preview).hexdigest(),
                    },
                ),
            }
        ),
        backup_store=MemoryWriteOnceStore(),
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path,
        ),
        now=lambda: NOW,
    )
    payload = service.create_verified(
        PurgeObjectBackupRequest("batch-001", ("asset-a",))
    ).manifest.to_dict()
    if mutation == "unknown_field":
        payload["unexpected"] = True
    elif mutation == "verified_gate":
        payload["production_gates"]["backup_credential_no_delete"] = "verified"
    elif mutation == "unsafe_backup_key":
        payload["copies"]["objects"][0]["backup"]["key"] = "../escape"
    else:
        payload["copies"]["objects"].append(
            dict(payload["copies"]["objects"][0])
        )

    with pytest.raises(PurgeObjectIntegrityError) as caught:
        PurgeObjectBackupManifest.from_dict(payload)

    assert caught.value.error_code == "invalid_manifest"


def test_stale_reference_snapshot_fails_before_any_object_storage_access(
    tmp_path,
):
    original = b"source-a"
    preview = b"preview-a"
    snapshot = replace(
        _snapshot(original, preview),
        captured_at=NOW - timedelta(minutes=6),
    )
    backup = MemoryWriteOnceStore()
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=FakeReferenceSnapshotReader(snapshot),
        formal_objects=MemoryObjectReader({}),
        backup_store=backup,
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path,
        ),
        now=lambda: NOW,
    )

    with pytest.raises(PurgeObjectReferenceError) as caught:
        service.create_verified(PurgeObjectBackupRequest("batch-001", ("asset-a",)))

    assert caught.value.error_code == "reference_snapshot_stale"
    assert backup.objects == {}


@pytest.mark.parametrize(
    ("restore_point", "error_code"),
    [
        (
            _restore_point_manifest("other-batch"),
            "restore_point_batch_mismatch",
        ),
        (
            replace(
                _restore_point_manifest(),
                remote_bucket="different-backup-bucket",
            ),
            "restore_point_storage_mismatch",
        ),
    ],
)
def test_database_restore_point_must_match_batch_and_object_backup_bucket(
    tmp_path,
    restore_point,
    error_code,
):
    backup = MemoryWriteOnceStore()
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(restore_point),
        references=FakeReferenceSnapshotReader(None),
        formal_objects=MemoryObjectReader({}),
        backup_store=backup,
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path,
        ),
        now=lambda: NOW,
    )

    with pytest.raises(PurgeObjectIntegrityError) as caught:
        service.create_verified(PurgeObjectBackupRequest("batch-001", ("asset-a",)))

    assert caught.value.error_code == error_code
    assert backup.objects == {}


def test_retry_recovers_remote_final_when_local_commit_was_interrupted(
    tmp_path,
    monkeypatch,
):
    original = b"source-a"
    preview = b"preview-a"
    current_time = [NOW]
    references = FakeReferenceSnapshotReader(_snapshot(original, preview))
    backup = MemoryWriteOnceStore()
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=references,
        formal_objects=MemoryObjectReader(
            {
                "formal/a.png": (
                    original,
                    {
                        "sha256": hashlib.sha256(original).hexdigest(),
                        "source-size": str(len(original)),
                    },
                ),
                "formal/p.jpg": (
                    preview,
                    {
                        "preview-size": str(len(preview)),
                        "preview-md5": hashlib.md5(preview).hexdigest(),
                    },
                ),
            }
        ),
        backup_store=backup,
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path,
        ),
        now=lambda: current_time[0],
    )
    real_local_write = purge_module._write_local_immutable

    def fail_local_final(path, data):
        if path.name == "manifest.json":
            raise OSError("fake local commit interruption")
        real_local_write(path, data)

    monkeypatch.setattr(purge_module, "_write_local_immutable", fail_local_final)
    with pytest.raises(OSError):
        service.create_verified(PurgeObjectBackupRequest("batch-001", ("asset-a",)))
    monkeypatch.setattr(purge_module, "_write_local_immutable", real_local_write)
    writes = tuple(backup.writes)
    current_time[0] = NOW + timedelta(minutes=2)
    references.snapshot = replace(
        references.snapshot,
        captured_at=current_time[0],
    )

    result = service.create_verified(
        PurgeObjectBackupRequest("batch-001", ("asset-a",))
    )

    assert result.status == "complete"
    assert tuple(backup.writes) == writes
    assert (tmp_path / "purge-batch-001" / "objects" / "manifest.json").is_file()


@pytest.mark.parametrize("changed_key", ["formal/a.png", "formal/p.jpg"])
def test_complete_retry_rechecks_current_formal_object_bytes(
    tmp_path,
    changed_key,
):
    original = b"source-a"
    preview = b"preview-a"
    formal = MemoryObjectReader(
        {
            "formal/a.png": (
                original,
                {
                    "sha256": hashlib.sha256(original).hexdigest(),
                    "source-size": str(len(original)),
                },
            ),
            "formal/p.jpg": (
                preview,
                {
                    "preview-size": str(len(preview)),
                    "preview-md5": hashlib.md5(preview).hexdigest(),
                },
            ),
        }
    )
    backup = MemoryWriteOnceStore()
    service = PurgeObjectBackupService(
        restore_points=FakeRestorePointGate(_restore_point_manifest()),
        references=FakeReferenceSnapshotReader(_snapshot(original, preview)),
        formal_objects=formal,
        backup_store=backup,
        config=PurgeObjectBackupConfig(
            formal_bucket="private-image-assets",
            backup_bucket="private-backups",
            backup_prefix="postgresql-backups",
            local_root=tmp_path,
        ),
        now=lambda: NOW,
    )
    service.create_verified(PurgeObjectBackupRequest("batch-001", ("asset-a",)))
    writes = tuple(backup.writes)
    if changed_key.endswith("a.png"):
        changed = b"changed!"
        metadata = {
            "sha256": hashlib.sha256(changed).hexdigest(),
            "source-size": str(len(changed)),
        }
    else:
        changed = b"changed!!"
        metadata = {
            "preview-size": str(len(changed)),
            "preview-md5": hashlib.md5(changed).hexdigest(),
        }
    formal.objects[changed_key] = (changed, metadata)

    with pytest.raises(PurgeObjectIntegrityError):
        service.create_verified(PurgeObjectBackupRequest("batch-001", ("asset-a",)))

    assert tuple(backup.writes) == writes
