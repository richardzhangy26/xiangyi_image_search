import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from io import StringIO

import pytest

from scripts import manage_purge_object_backups as cli
from services.purge_object_backup import (
    PurgeObjectBackupItem,
    PurgeObjectBackupManifest,
)


NOW = datetime(2026, 8, 9, 5, 0, tzinfo=timezone.utc)


def _manifest():
    formal_key = "formal/a.png"
    object_id = hashlib.sha256(
        f"private-image-assets\0{formal_key}".encode()
    ).hexdigest()
    base = "postgresql-backups/purge-batch-001/objects"
    item = PurgeObjectBackupItem(
        object_id=object_id,
        kind="source_image",
        asset_ids=("asset-a",),
        formal_bucket="private-image-assets",
        formal_key=formal_key,
        backup_bucket="private-backups",
        backup_key=f"{base}/payloads/source_image/{object_id}",
        size=8,
        sha256="a" * 64,
        selected_reference_count=1,
        total_reference_count=1,
        remaining_reference_count=0,
        reference_set_sha256="b" * 64,
        verification={
            "source_head_download": "passed",
            "backup_head": "passed",
            "backup_download_sha256": "passed",
        },
    )
    return PurgeObjectBackupManifest(
        schema_version=1,
        status="complete",
        kind="purge_object_backup",
        purge_batch_id="batch-001",
        database_restore_point={
            "backup_id": "purge-batch-001",
            "purge_batch_id": "batch-001",
            "remote_bucket": "private-backups",
            "remote_manifest_key": (
                "postgresql-backups/purge-batch-001/manifest.json"
            ),
            "manifest_sha256": "c" * 64,
            "artifact_sha256": "d" * 64,
            "completed_at": NOW.isoformat().replace("+00:00", "Z"),
            "retain_until": (
                NOW + timedelta(days=30)
            ).isoformat().replace("+00:00", "Z"),
        },
        asset_ids=("asset-a",),
        reference_catalog_version=1,
        reference_snapshot_sha256="e" * 64,
        plan_key=f"{base}/plan.json",
        manifest_key=f"{base}/manifest.json",
        objects=(item,),
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


def _environment(tmp_path):
    return {
        "BACKUP_OSS_ACCESS_KEY_ID": "backup-access",
        "BACKUP_OSS_ACCESS_KEY_SECRET": "backup-secret",
        "BACKUP_OSS_ENDPOINT": "oss.example.invalid",
        "BACKUP_OSS_BUCKET_NAME": "private-backups",
        "BACKUP_OSS_BASE_PREFIX": "postgresql-backups",
        "OSS_ACCESS_KEY_ID": "application-access",
        "OSS_BUCKET_NAME": "private-image-assets",
        "BACKUP_ROOT": str(tmp_path),
    }


class Result:
    def __init__(self, payload):
        self.payload = payload

    def to_dict(self):
        return self.payload


class FakeService:
    def __init__(self, calls, **kwargs):
        self.calls = calls
        self.calls.append(("service", kwargs))

    def verify_copies(self, manifest):
        self.calls.append(("verify", manifest.purge_batch_id))
        return Result({"status": "verified", "object_count": 1})

    def restore_to_isolation(
        self,
        manifest,
        *,
        restore_run_id,
        acknowledge_isolated,
    ):
        self.calls.append(
            ("restore", restore_run_id, acknowledge_isolated)
        )
        return Result({"status": "verified", "object_count": 1})


def test_parser_exposes_only_verify_and_isolated_restore_commands():
    parser = cli.create_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )

    assert set(subparsers.choices) == {"verify-copies", "restore-isolated"}
    with pytest.raises(cli.PurgeObjectCliConfigError):
        parser.parse_args(["create-backup"])
    with pytest.raises(cli.PurgeObjectCliConfigError):
        parser.parse_args(["delete"])


def test_main_redacts_unknown_command_as_stable_json():
    error = StringIO()

    result = cli.main(["delete"], environ={}, stdout=StringIO(), stderr=error)

    assert result == cli.EXIT_CONFIG
    payload = json.loads(error.getvalue())
    assert payload["stage"] == "config"
    assert payload["error_code"] == "invalid_arguments"
    assert "delete" not in payload["error"]


def test_verify_command_does_not_construct_source_or_isolation_adapters(
    tmp_path,
):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest().to_dict()), encoding="utf-8")
    calls = []
    output = StringIO()

    result = cli.main(
        ["verify-copies", "--manifest", str(path)],
        environ=_environment(tmp_path),
        backup_storage_factory=lambda environment: calls.append("backup")
        or object(),
        isolation_storage_factory=lambda environment: pytest.fail(
            "verify-copies 不得构造隔离写 Adapter"
        ),
        service_factory=lambda **kwargs: FakeService(calls, **kwargs),
        stdout=output,
        stderr=StringIO(),
    )

    assert result == cli.EXIT_SUCCESS
    assert calls[0] == "backup"
    assert ("verify", "batch-001") in calls
    assert json.loads(output.getvalue())["status"] == "verified"


def test_restore_command_constructs_only_backup_and_isolation_roles(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest().to_dict()), encoding="utf-8")
    environment = _environment(tmp_path)
    environment.update(
        {
            "PURGE_RESTORE_OSS_ACCESS_KEY_ID": "restore-access",
            "PURGE_RESTORE_OSS_ACCESS_KEY_SECRET": "restore-secret",
            "PURGE_RESTORE_OSS_ENDPOINT": "oss.example.invalid",
            "PURGE_RESTORE_OSS_BUCKET_NAME": "disposable-restore",
            "PURGE_RESTORE_OSS_BASE_PREFIX": "isolated-restores",
            "PURGE_RESTORE_ISOLATED": "1",
        }
    )
    calls = []

    result = cli.main(
        [
            "restore-isolated",
            "--manifest",
            str(path),
            "--restore-run-id",
            "drill-001",
            "--acknowledge-isolated",
        ],
        environ=environment,
        backup_storage_factory=lambda value: calls.append("backup") or object(),
        isolation_storage_factory=lambda value: calls.append("isolation")
        or object(),
        service_factory=lambda **kwargs: FakeService(calls, **kwargs),
        stdout=StringIO(),
        stderr=StringIO(),
    )

    assert result == cli.EXIT_SUCCESS
    assert calls[:2] == ["backup", "isolation"]
    assert ("restore", "drill-001", True) in calls
