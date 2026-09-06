import hashlib
import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest


def _module():
    return importlib.import_module("services.postgres_backup")


class FakeStorage:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.downloads = []

    def download_to(self, key, target):
        self.downloads.append(key)
        target.write(self.objects[key])


class FakeRestoreRunner:
    def __init__(self):
        self.calls = []
        self.database_exists = False
        self.fail_program = None
        self.structure = {
            "vector_extension": True,
            "products_table": True,
            "image_assets_table": True,
            "vector_type": "vector(1024)",
            "products_count": 2,
            "image_assets_count": 3,
        }

    def run(self, argv, *, env, timeout):
        argv = tuple(str(item) for item in argv)
        recorded_env = {
            name: env.get(name)
            for name in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")
        }
        self.calls.append(SimpleNamespace(argv=argv, env=recorded_env, timeout=timeout))
        program = Path(argv[0]).name
        if self.fail_program == program and "--version" not in argv:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"restore-secret")
        if "--version" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout=f"{program} (PostgreSQL) 16.3\n".encode(),
                stderr=b"",
            )
        if program == "createdb":
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if program == "pg_restore":
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if program == "psql":
            command = next(item for item in argv if item.startswith("--command="))
            if "FROM pg_database" in command:
                return SimpleNamespace(
                    returncode=0,
                    stdout=b"1\n" if self.database_exists else b"\n",
                    stderr=b"",
                )
            if "pg_control_system" in command:
                return SimpleNamespace(
                    returncode=0,
                    stdout=b"restore-system-002|postgres|160004\n",
                    stderr=b"",
                )
            return SimpleNamespace(
                returncode=0,
                stdout=(json.dumps(self.structure) + "\n").encode(),
                stderr=b"",
            )
        raise AssertionError(f"unexpected program {program}")


def _manifest(data=b"PGDMP-remote"):
    module = _module()
    created_at = datetime(2026, 8, 6, tzinfo=timezone.utc)
    return module.BackupManifest(
        schema_version=1,
        status="complete",
        backup_id="purge-batch-001",
        kind="purge_restore_point",
        purge_batch_id="batch-001",
        created_at=created_at,
        completed_at=created_at,
        database_identity={
            "host": "source.internal",
            "port": 5432,
            "database": "image_search",
            "system_identifier": "source-system-001",
        },
        postgres_client_major=16,
        postgres_server_major=16,
        artifact_file="backup.dump",
        artifact_size=len(data),
        artifact_sha256=hashlib.sha256(data).hexdigest(),
        local_relative_path="purge-batch-001/backup.dump",
        remote_bucket="private-database-backups",
        remote_dump_key="postgresql-backups/purge-batch-001/backup.dump",
        remote_manifest_key="postgresql-backups/purge-batch-001/manifest.json",
        retention_days=30,
        retain_until=created_at + timedelta(days=30),
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


def _config(module, *, disposable=True, host="restore.internal"):
    return module.RestoreVerificationConfig(
        host=host,
        port=5432,
        maintenance_database="postgres",
        user="restore_admin",
        password="restore-secret",
        disposable=disposable,
    )


def _verifier(tmp_path, *, runner=None, storage=None, config=None):
    module = _module()
    data = b"PGDMP-remote"
    manifest = _manifest(data)
    manifest_bytes = (
        json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode()
    return (
        module.PostgresRestoreVerifier(
            runner=runner or FakeRestoreRunner(),
            storage=storage
            or FakeStorage(
                {
                    manifest.remote_dump_key: data,
                    manifest.remote_manifest_key: manifest_bytes,
                }
            ),
            config=config or _config(module),
            temporary_root=tmp_path,
            remote_bucket="private-database-backups",
            remote_prefix="postgresql-backups",
            uuid_factory=lambda: UUID("12345678-1234-5678-1234-567812345678"),
        ),
        manifest,
    )


def test_restore_config_never_falls_back_to_application_database_names():
    module = _module()
    with pytest.raises(module.BackupConfigError, match="RESTORE_VERIFY_DB_HOST"):
        module.RestoreVerificationConfig.from_env(
            {
                "DB_HOST": "production",
                "RESTORE_VERIFY_DISPOSABLE": "1",
            }
        )


def test_restore_requires_disposable_flag_and_acknowledgement(tmp_path):
    module = _module()
    verifier, manifest = _verifier(tmp_path)
    with pytest.raises(module.RestoreSafetyError, match="确认"):
        verifier.verify_from_remote(manifest, acknowledge_isolated=False)

    verifier, manifest = _verifier(
        tmp_path,
        config=_config(module, disposable=False),
    )
    with pytest.raises(module.RestoreSafetyError, match="DISPOSABLE"):
        verifier.verify_from_remote(manifest, acknowledge_isolated=True)


def test_restore_rejects_same_host_or_same_system_identity(tmp_path):
    module = _module()
    verifier, manifest = _verifier(
        tmp_path,
        config=_config(module, host="source.internal"),
    )
    with pytest.raises(module.RestoreSafetyError, match="源数据库"):
        verifier.verify_from_remote(manifest, acknowledge_isolated=True)

    runner = FakeRestoreRunner()
    runner.calls = []
    _unused_verifier, manifest = _verifier(tmp_path, runner=runner)
    manifest = module.BackupManifest(
        **{
            **manifest.__dict__,
            "database_identity": {
                **manifest.database_identity,
                "system_identifier": "restore-system-002",
            },
        }
    )
    manifest_bytes = (
        json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode()
    storage = FakeStorage(
        {
            manifest.remote_dump_key: b"PGDMP-remote",
            manifest.remote_manifest_key: manifest_bytes,
        }
    )
    verifier, _unused_manifest = _verifier(
        tmp_path,
        runner=runner,
        storage=storage,
    )
    with pytest.raises(module.RestoreSafetyError, match="源数据库"):
        verifier.verify_from_remote(manifest, acknowledge_isolated=True)


def test_restore_uses_generated_absent_database_and_never_drops(tmp_path):
    runner = FakeRestoreRunner()
    verifier, manifest = _verifier(tmp_path, runner=runner)

    result = verifier.verify_from_remote(manifest, acknowledge_isolated=True)

    assert result.status == "verified"
    assert result.target_database == "backup_verify_12345678123456781234567812345678"
    programs = [Path(call.argv[0]).name for call in runner.calls]
    assert "createdb" in programs
    assert "pg_restore" in programs
    assert "dropdb" not in programs


def test_restore_command_forbids_clean_create_and_password_in_argv(tmp_path):
    runner = FakeRestoreRunner()
    verifier, manifest = _verifier(tmp_path, runner=runner)
    verifier.verify_from_remote(manifest, acknowledge_isolated=True)

    restore = next(
        call
        for call in runner.calls
        if Path(call.argv[0]).name == "pg_restore" and "--version" not in call.argv
    )
    assert "--exit-on-error" in restore.argv
    assert "--single-transaction" in restore.argv
    assert "--no-owner" in restore.argv
    assert "--no-acl" in restore.argv
    assert "--clean" not in restore.argv
    assert "--create" not in restore.argv
    assert all("restore-secret" not in " ".join(call.argv) for call in runner.calls)
    assert all(call.env["PGPASSWORD"] == "restore-secret" for call in runner.calls)


def test_existing_generated_target_fails_before_createdb_or_restore(tmp_path):
    module = _module()
    runner = FakeRestoreRunner()
    runner.database_exists = True
    verifier, manifest = _verifier(tmp_path, runner=runner)

    with pytest.raises(module.RestoreSafetyError, match="已存在"):
        verifier.verify_from_remote(manifest, acknowledge_isolated=True)

    assert all(
        not (Path(call.argv[0]).name == "createdb" and "--version" not in call.argv)
        for call in runner.calls
    )
    assert all(
        not (Path(call.argv[0]).name == "pg_restore" and "--version" not in call.argv)
        for call in runner.calls
    )


def test_restore_failure_returns_stable_result_and_keeps_database(tmp_path):
    runner = FakeRestoreRunner()
    runner.fail_program = "pg_restore"
    verifier, manifest = _verifier(tmp_path, runner=runner)

    result = verifier.verify_from_remote(manifest, acknowledge_isolated=True)

    assert result.status == "failed"
    assert result.stage == "restore"
    assert result.error_code == "pg_restore_failed"
    assert all(Path(call.argv[0]).name != "dropdb" for call in runner.calls)
    assert "restore-secret" not in json.dumps(result.to_dict())


def test_vector_dimension_mismatch_is_a_structural_failure(tmp_path):
    runner = FakeRestoreRunner()
    runner.structure["vector_type"] = "vector(768)"
    verifier, manifest = _verifier(tmp_path, runner=runner)

    result = verifier.verify_from_remote(manifest, acknowledge_isolated=True)

    assert result.status == "failed"
    assert result.stage == "structure_verify"
    assert result.evidence["vector_type"] == "vector(768)"


def test_restore_refuses_corrupt_remote_dump_before_database_creation(tmp_path):
    module = _module()
    runner = FakeRestoreRunner()
    manifest = _manifest()
    manifest_bytes = (
        json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode()
    storage = FakeStorage(
        {
            manifest.remote_dump_key: b"corrupt",
            manifest.remote_manifest_key: manifest_bytes,
        }
    )
    verifier, _ = _verifier(tmp_path, runner=runner, storage=storage)

    with pytest.raises(module.BackupIntegrityError, match="异机备份"):
        verifier.verify_from_remote(manifest, acknowledge_isolated=True)
    assert all(
        not (Path(call.argv[0]).name == "createdb" and "--version" not in call.argv)
        for call in runner.calls
    )


def test_restore_rejects_remote_manifest_mismatch_before_database_creation(tmp_path):
    module = _module()
    runner = FakeRestoreRunner()
    manifest = _manifest()
    storage = FakeStorage(
        {
            manifest.remote_dump_key: b"PGDMP-remote",
            manifest.remote_manifest_key: b'{"status":"complete"}\n',
        }
    )
    verifier, _ = _verifier(tmp_path, runner=runner, storage=storage)

    with pytest.raises(module.BackupIntegrityError, match="异机 final manifest"):
        verifier.verify_from_remote(manifest, acknowledge_isolated=True)
    assert all(
        not (Path(call.argv[0]).name == "createdb" and "--version" not in call.argv)
        for call in runner.calls
    )
