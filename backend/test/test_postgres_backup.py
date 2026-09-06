import importlib
import json
import stat
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


def _module():
    try:
        return importlib.import_module("services.postgres_backup")
    except ModuleNotFoundError:
        pytest.fail("services.postgres_backup 尚未实现")


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.database_name = "image_search"
        self.server_version_num = "160004"
        self.client_major = 16
        self.fail_program = None

    def run(self, argv, *, env, timeout):
        argv = tuple(str(item) for item in argv)
        recorded_env = {
            name: env.get(name)
            for name in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")
        }
        self.calls.append(SimpleNamespace(argv=argv, env=recorded_env, timeout=timeout))
        program = Path(argv[0]).name
        if self.fail_program == program:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"secret detail")
        if "--version" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout=f"{program} (PostgreSQL) {self.client_major}.3\n".encode(),
                stderr=b"",
            )
        if program == "psql":
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"{self.database_name}|{self.server_version_num}|source-system-001\n"
                ).encode(),
                stderr=b"",
            )
        if program == "pg_dump":
            output_arg = next(arg for arg in argv if arg.startswith("--file="))
            dump_path = Path(output_arg.split("=", 1)[1])
            dump_path.write_bytes(b"PGDMP-fake-custom-archive")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if program == "pg_restore":
            return SimpleNamespace(returncode=0, stdout=b"TOC\n", stderr=b"")
        raise AssertionError(f"unexpected program: {program}")


class FakeStorage:
    def __init__(self):
        self.objects = {}
        self.events = []
        self.put_calls = []
        self.corrupt_dump_download = False
        self.fail_head_once = False

    def head(self, key):
        if self.fail_head_once:
            self.fail_head_once = False
            raise RuntimeError("simulated storage outage")
        self.events.append(("head", key))
        item = self.objects.get(key)
        if item is None:
            return None
        data, metadata = item
        return SimpleNamespace(key=key, size=len(data), metadata=dict(metadata))

    def put_file_if_absent(self, key, path, *, metadata):
        assert key not in self.objects
        data = Path(path).read_bytes()
        self.objects[key] = (data, dict(metadata))
        self.events.append(("put_file", key))
        self.put_calls.append(key)

    def put_bytes_if_absent(self, key, data, *, metadata):
        assert key not in self.objects
        self.objects[key] = (bytes(data), dict(metadata))
        self.events.append(("put_bytes", key))
        self.put_calls.append(key)

    def download_to(self, key, target):
        self.events.append(("download", key))
        data = self.objects[key][0]
        if self.corrupt_dump_download and key.endswith("backup.dump"):
            data += b"corrupt"
        target.write(data)


def _source_config(module, *, database="image_search"):
    return module.PostgresConnectionConfig(
        host="db.internal",
        port=5432,
        database=database,
        user="backup_reader",
        password="backup-secret",
    )


def _service(tmp_path, *, runner=None, storage=None, database="image_search"):
    module = _module()
    return module.PostgresBackupService(
        runner=runner or FakeRunner(),
        storage=storage or FakeStorage(),
        source=_source_config(module, database=database),
        backup_root=tmp_path / "backups",
        remote_bucket="private-database-backups",
        remote_prefix="postgresql-backups",
        now=lambda: datetime(2026, 8, 6, 3, 4, 5, tzinfo=timezone.utc),
    )


def test_connection_config_requires_dedicated_backup_names():
    module = _module()
    with pytest.raises(module.BackupConfigError, match="BACKUP_DB_HOST"):
        module.PostgresConnectionConfig.from_env(
            {
                "DB_HOST": "production",
                "DB_NAME": "image_search",
                "DB_USER": "application",
                "DB_PASSWORD": "application-secret",
            },
            prefix="BACKUP_DB_",
        )


def test_request_ids_are_stable_and_reject_path_characters():
    module = _module()
    assert module.BackupRequest.daily(date(2026, 8, 6)).backup_id == "daily-2026-08-06"
    restore_point = module.BackupRequest.restore_point("purge-batch-001")
    assert restore_point.backup_id == "purge-purge-batch-001"
    assert restore_point.purge_batch_id == "purge-batch-001"

    with pytest.raises(module.BackupConfigError, match="批次标识"):
        module.BackupRequest.restore_point("../escape")


def test_password_is_only_in_explicit_process_environment(tmp_path):
    runner = FakeRunner()
    service = _service(tmp_path, runner=runner)
    module = _module()

    service.create_backup(module.BackupRequest.daily(date(2026, 8, 6)))

    assert all("backup-secret" not in " ".join(call.argv) for call in runner.calls)
    assert all(call.env["PGPASSWORD"] == "backup-secret" for call in runner.calls)


def test_complete_manifest_is_last_commit_marker_and_is_redacted(tmp_path):
    storage = FakeStorage()
    service = _service(tmp_path, storage=storage)
    module = _module()

    result = service.create_backup(
        module.BackupRequest.restore_point("purge-batch-001")
    )
    manifest_path = tmp_path / "backups" / result.manifest.backup_id / "manifest.json"
    payload = manifest_path.read_text(encoding="utf-8")

    assert result.status == "complete"
    assert storage.events[-1] == ("download", result.manifest.remote_manifest_key)
    assert json.loads(payload)["status"] == "complete"
    assert "backup-secret" not in payload
    assert "postgresql://" not in payload
    assert result.manifest.retention_days == 30
    assert set(result.manifest.production_gates.values()) == {"not_verified"}


def test_remote_hash_failure_leaves_failed_attempt_without_manifest(tmp_path):
    storage = FakeStorage()
    storage.corrupt_dump_download = True
    service = _service(tmp_path, storage=storage)
    module = _module()
    request = module.BackupRequest.daily(date(2026, 8, 6))

    with pytest.raises(module.BackupIntegrityError, match="异机备份哈希"):
        service.create_backup(request)

    directory = tmp_path / "backups" / request.backup_id
    assert not (directory / "manifest.json").exists()
    attempt = json.loads((directory / "attempt-result.json").read_text())
    assert attempt["status"] == "failed"
    assert attempt["stage"] == "remote_dump_verify"


def test_retry_reconciles_identical_complete_backup_without_put(tmp_path):
    storage = FakeStorage()
    service = _service(tmp_path, storage=storage)
    module = _module()
    request = module.BackupRequest.restore_point("batch-001")
    first = service.create_backup(request)
    put_calls = list(storage.put_calls)

    second = service.create_backup(request)

    assert second.manifest.to_dict() == first.manifest.to_dict()
    assert storage.put_calls == put_calls


def test_retry_resumes_partial_backup_after_remote_verification_failure(tmp_path):
    storage = FakeStorage()
    storage.corrupt_dump_download = True
    runner = FakeRunner()
    service = _service(tmp_path, storage=storage, runner=runner)
    module = _module()
    request = module.BackupRequest.restore_point("batch-partial")

    with pytest.raises(module.BackupIntegrityError):
        service.create_backup(request)
    dump_calls_before_retry = sum(
        Path(call.argv[0]).name == "pg_dump" and "--version" not in call.argv
        for call in runner.calls
    )

    storage.corrupt_dump_download = False
    result = service.create_backup(request)

    assert result.status == "complete"
    assert sum(
        Path(call.argv[0]).name == "pg_dump" and "--version" not in call.argv
        for call in runner.calls
    ) == dump_calls_before_retry


def test_retry_reuses_candidate_manifest_after_local_commit_crash(tmp_path, monkeypatch):
    storage = FakeStorage()
    module = _module()
    moments = iter(
        [
            datetime(2026, 8, 6, 3, 4, 5, tzinfo=timezone.utc),
            datetime(2026, 8, 6, 3, 5, 5, tzinfo=timezone.utc),
            datetime(2026, 8, 6, 3, 6, 5, tzinfo=timezone.utc),
        ]
    )
    service = module.PostgresBackupService(
        runner=FakeRunner(),
        storage=storage,
        source=_source_config(module),
        backup_root=tmp_path / "backups",
        remote_bucket="private-database-backups",
        remote_prefix="postgresql-backups",
        now=lambda: next(moments),
    )
    request = module.BackupRequest.restore_point("candidate-retry")
    original_replace = module.os.replace
    failed_once = False

    def fail_local_manifest_once(source, destination):
        nonlocal failed_once
        if Path(destination).name == "manifest.json" and not failed_once:
            failed_once = True
            raise OSError("simulated local commit crash")
        return original_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_local_manifest_once)
    with pytest.raises(OSError, match="simulated"):
        service.create_backup(request)

    result = service.create_backup(request)

    assert result.status == "complete"
    assert result.manifest.completed_at == datetime(
        2026, 8, 6, 3, 5, 5, tzinfo=timezone.utc
    )


def test_retry_rejects_modified_partial_local_dump_before_remote_upload(tmp_path):
    storage = FakeStorage()
    storage.fail_head_once = True
    module = _module()
    service = _service(tmp_path, storage=storage)
    request = module.BackupRequest.restore_point("immutable-partial")

    with pytest.raises(RuntimeError, match="storage outage"):
        service.create_backup(request)
    dump_path = tmp_path / "backups" / request.backup_id / "backup.dump"
    dump_path.write_bytes(b"different-local-dump")

    with pytest.raises(module.BackupConflictError, match="本机 partial"):
        service.create_backup(request)
    assert storage.put_calls == []


def test_retry_rejects_same_id_for_different_database_identity(tmp_path):
    storage = FakeStorage()
    module = _module()
    request = module.BackupRequest.daily(date(2026, 8, 6))
    _service(tmp_path, storage=storage).create_backup(request)

    with pytest.raises(module.BackupConflictError, match="数据库身份"):
        _service(tmp_path, storage=storage, database="other_db").create_backup(request)


def test_local_files_are_private_and_verify_copies_checks_both(tmp_path):
    storage = FakeStorage()
    service = _service(tmp_path, storage=storage)
    module = _module()
    result = service.create_backup(module.BackupRequest.daily(date(2026, 8, 6)))
    directory = tmp_path / "backups" / result.manifest.backup_id

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "backup.dump").stat().st_mode) == 0o600
    verified = service.verify_copies(directory / "manifest.json")
    assert verified.status == "verified"
    assert storage.events[-1] == ("download", result.manifest.remote_manifest_key)


def test_verify_rejects_remote_manifest_that_differs_from_local_commit_marker(tmp_path):
    storage = FakeStorage()
    service = _service(tmp_path, storage=storage)
    module = _module()
    result = service.create_backup(module.BackupRequest.daily(date(2026, 8, 6)))
    remote_manifest_key = result.manifest.remote_manifest_key
    _data, metadata = storage.objects[remote_manifest_key]
    storage.objects[remote_manifest_key] = (b'{"status":"complete"}\n', metadata)
    local_manifest = tmp_path / "backups" / result.manifest.backup_id / "manifest.json"

    with pytest.raises(module.BackupIntegrityError, match="异机 final manifest"):
        service.verify_copies(local_manifest)


def test_verify_rejects_manifest_object_keys_outside_configured_prefix(tmp_path):
    storage = FakeStorage()
    service = _service(tmp_path, storage=storage)
    module = _module()
    result = service.create_backup(module.BackupRequest.daily(date(2026, 8, 6)))
    local_manifest = tmp_path / "backups" / result.manifest.backup_id / "manifest.json"
    payload = json.loads(local_manifest.read_text())
    payload["copies"]["remote"]["dump_key"] = (
        f"other-prefix/{result.manifest.backup_id}/backup.dump"
    )
    local_manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(module.BackupIntegrityError, match="manifest.*对象键"):
        service.verify_copies(local_manifest)


def test_manifest_contract_rejects_purge_batch_binding_mismatch():
    module = _module()
    manifest = _manifest_payload_for_contract_test()
    manifest["backup_id"] = "purge-other-batch"

    with pytest.raises(module.BackupIntegrityError, match="manifest.*批次"):
        module.BackupManifest.from_dict(manifest)


def test_verify_rejects_partial_or_unknown_manifest(tmp_path):
    module = _module()
    service = _service(tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 999, "status": "failed"}))

    with pytest.raises(module.BackupIntegrityError, match="manifest"):
        service.verify_copies(manifest)


def test_subprocess_runner_never_uses_shell(monkeypatch):
    module = _module()
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.SubprocessCommandRunner().run(
        ["pg_dump", "--version"], env={"PGPASSWORD": "secret"}, timeout=3
    )

    assert captured["argv"] == ["pg_dump", "--version"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 3


def test_subprocess_timeout_becomes_stable_command_result(monkeypatch):
    module = _module()

    def timeout(*_args, **_kwargs):
        raise module.subprocess.TimeoutExpired(cmd=["pg_dump"], timeout=3)

    monkeypatch.setattr(module.subprocess, "run", timeout)
    result = module.SubprocessCommandRunner().run(
        ["pg_dump", "--version"], env={"PGPASSWORD": "secret"}, timeout=3
    )

    assert result.returncode == 124
    assert result.stdout == b""
    assert result.stderr == b""


def test_manifest_cannot_self_assert_external_production_gates():
    module = _module()
    manifest = _manifest_payload_for_contract_test()
    manifest["production_gates"]["remote_restore_drill"] = "verified"

    with pytest.raises(module.BackupIntegrityError, match="production gate"):
        module.BackupManifest.from_dict(manifest)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest["artifact"].__setitem__("format", "plain-sql"),
        lambda manifest: manifest["database_identity"].__setitem__(
            "unexpected", "value"
        ),
    ],
)
def test_manifest_rejects_unknown_format_or_fields(mutate):
    module = _module()
    manifest = _manifest_payload_for_contract_test()
    mutate(manifest)

    with pytest.raises(module.BackupIntegrityError, match="manifest"):
        module.BackupManifest.from_dict(manifest)


def _manifest_payload_for_contract_test():
    created_at = "2026-08-06T03:04:05Z"
    return {
        "schema_version": 1,
        "status": "complete",
        "backup_id": "purge-batch-001",
        "kind": "purge_restore_point",
        "purge_batch_id": "batch-001",
        "created_at": created_at,
        "completed_at": created_at,
        "database_identity": {
            "host": "source.internal",
            "port": 5432,
            "database": "image_search",
            "system_identifier": "source-system-001",
        },
        "postgres": {"client_major": 16, "server_major": 16},
        "artifact": {
            "file": "backup.dump",
            "format": "postgresql-custom",
            "size_bytes": 10,
            "sha256": "a" * 64,
        },
        "copies": {
            "local": {"relative_path": "purge-batch-001/backup.dump"},
            "remote": {
                "bucket": "private-database-backups",
                "dump_key": "postgresql-backups/purge-batch-001/backup.dump",
                "manifest_key": "postgresql-backups/purge-batch-001/manifest.json",
            },
        },
        "retention": {
            "days": 30,
            "retain_until": "2026-09-05T03:04:05Z",
        },
        "verification": {
            "local_pg_restore_list": "passed",
            "remote_dump_sha256": "passed",
            "remote_manifest_readback": "passed",
        },
        "production_gates": {
            "bucket_private": "not_verified",
            "server_side_encryption": "not_verified",
            "credential_least_privilege_no_delete": "not_verified",
            "retention_policy_30_days": "not_verified",
            "remote_restore_drill": "not_verified",
        },
    }
