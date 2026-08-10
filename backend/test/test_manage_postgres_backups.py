import importlib
import io
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


class UnifiedFakeRunner:
    def __init__(self):
        self.calls = []
        self.fail_program = None
        self.database_exists = False

    def run(self, argv, *, env, timeout):
        argv = tuple(str(item) for item in argv)
        recorded_env = {
            name: env.get(name)
            for name in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")
        }
        self.calls.append(SimpleNamespace(argv=argv, env=recorded_env, timeout=timeout))
        program = Path(argv[0]).name
        if self.fail_program == program and "--version" not in argv:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"secret-output")
        if "--version" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout=f"{program} (PostgreSQL) 16.3\n".encode(),
                stderr=b"",
            )
        if program == "pg_dump":
            output = next(item for item in argv if item.startswith("--file="))
            Path(output.split("=", 1)[1]).write_bytes(b"PGDMP-cli")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if program == "pg_restore":
            return SimpleNamespace(returncode=0, stdout=b"TOC\n", stderr=b"")
        if program == "createdb":
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if program == "psql":
            command = next(item for item in argv if item.startswith("--command="))
            if "FROM pg_database" in command:
                return SimpleNamespace(
                    returncode=0,
                    stdout=b"1\n" if self.database_exists else b"\n",
                    stderr=b"",
                )
            if "json_build_object" in command:
                evidence = {
                    "vector_extension": True,
                    "products_table": True,
                    "image_assets_table": True,
                    "vector_type": "vector(1024)",
                    "products_count": 1,
                    "image_assets_count": 1,
                }
                return SimpleNamespace(
                    returncode=0,
                    stdout=(json.dumps(evidence) + "\n").encode(),
                    stderr=b"",
                )
            if env["PGHOST"] == "restore.internal":
                output = b"restore-system-002|postgres|160004\n"
            else:
                output = b"image_search|160004|source-system-001\n"
            return SimpleNamespace(returncode=0, stdout=output, stderr=b"")
        raise AssertionError(f"unexpected program {program}")


class FakeStorage:
    def __init__(self):
        self.objects = {}

    def head(self, key):
        item = self.objects.get(key)
        if item is None:
            return None
        data, metadata = item
        return SimpleNamespace(key=key, size=len(data), metadata=dict(metadata))

    def put_file_if_absent(self, key, path, *, metadata):
        self.objects[key] = (Path(path).read_bytes(), dict(metadata))

    def put_bytes_if_absent(self, key, data, *, metadata):
        self.objects[key] = (bytes(data), dict(metadata))

    def download_to(self, key, target):
        target.write(self.objects[key][0])


def _environment(tmp_path):
    return {
        "BACKUP_DB_HOST": "source.internal",
        "BACKUP_DB_PORT": "5432",
        "BACKUP_DB_NAME": "image_search",
        "BACKUP_DB_USER": "backup_reader",
        "BACKUP_DB_PASSWORD": "backup-secret",
        "BACKUP_ROOT": str(tmp_path / "backups"),
        "BACKUP_DAILY_TIMEZONE": "Asia/Shanghai",
        "BACKUP_OSS_ACCESS_KEY_ID": "backup-access",
        "BACKUP_OSS_ACCESS_KEY_SECRET": "backup-oss-secret",
        "BACKUP_OSS_ENDPOINT": "oss-cn-shanghai.aliyuncs.com",
        "BACKUP_OSS_BUCKET_NAME": "private-database-backups",
        "BACKUP_OSS_BASE_PREFIX": "postgresql-backups",
        "BACKUP_OSS_SSE": "AES256",
        "OSS_ACCESS_KEY_ID": "application-access",
        "OSS_BUCKET_NAME": "private-image-assets",
        "RESTORE_VERIFY_DB_HOST": "restore.internal",
        "RESTORE_VERIFY_DB_PORT": "5432",
        "RESTORE_VERIFY_DB_NAME": "postgres",
        "RESTORE_VERIFY_DB_USER": "restore_admin",
        "RESTORE_VERIFY_DB_PASSWORD": "restore-secret",
        "RESTORE_VERIFY_DISPOSABLE": "1",
    }


def _run(argv, tmp_path, *, runner=None, storage=None, environment=None):
    try:
        module = importlib.import_module("scripts.manage_postgres_backups")
    except ModuleNotFoundError:
        pytest.fail("scripts.manage_postgres_backups 尚未实现")
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = module.main(
        argv,
        environ=environment or _environment(tmp_path),
        runner_factory=lambda: runner or UnifiedFakeRunner(),
        storage_factory=lambda _environment: storage or FakeStorage(),
        stdout=stdout,
        stderr=stderr,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_create_daily_and_restore_point_emit_complete_stable_json(tmp_path):
    runner = UnifiedFakeRunner()
    storage = FakeStorage()

    daily_code, daily_output, daily_error = _run(
        ["create-daily"], tmp_path, runner=runner, storage=storage
    )
    restore_code, restore_output, restore_error = _run(
        ["create-restore-point", "--purge-batch-id", "batch-001"],
        tmp_path,
        runner=runner,
        storage=storage,
    )

    assert daily_code == restore_code == 0
    assert json.loads(daily_output)["status"] == "complete"
    restore_payload = json.loads(restore_output)
    assert restore_payload["manifest"]["backup_id"] == "purge-batch-001"
    assert restore_payload["manifest"]["purge_batch_id"] == "batch-001"
    assert daily_error == restore_error == ""


def test_verify_copies_and_restore_use_existing_complete_manifest(tmp_path):
    runner = UnifiedFakeRunner()
    storage = FakeStorage()
    _run(["create-daily"], tmp_path, runner=runner, storage=storage)
    manifests = list((tmp_path / "backups").glob("daily-*/manifest.json"))
    assert len(manifests) == 1

    copies_code, copies_output, _ = _run(
        ["verify-copies", "--manifest", str(manifests[0])],
        tmp_path,
        runner=runner,
        storage=storage,
    )
    restore_code, restore_output, _ = _run(
        [
            "verify-restore",
            "--manifest",
            str(manifests[0]),
            "--acknowledge-isolated",
        ],
        tmp_path,
        runner=runner,
        storage=storage,
    )

    assert copies_code == restore_code == 0
    assert json.loads(copies_output)["status"] == "verified"
    assert json.loads(restore_output)["status"] == "verified"


def test_missing_dedicated_config_returns_exit_2(tmp_path):
    code, output, error = _run(
        ["create-daily"],
        tmp_path,
        environment={"DB_HOST": "production", "DB_PASSWORD": "app-secret"},
    )
    payload = json.loads(error)

    assert code == 2
    assert output == ""
    assert payload["status"] == "failed"
    assert payload["stage"] == "config"
    assert "app-secret" not in error


def test_cli_usage_error_returns_stable_json_exit_2(tmp_path):
    code, output, error = _run(
        ["create-restore-point"],
        tmp_path,
    )

    assert code == 2
    assert output == ""
    payload = json.loads(error)
    assert payload["status"] == "failed"
    assert payload["stage"] == "config"
    assert payload["error_code"] == "invalid_arguments"


def test_direct_script_entrypoint_returns_stable_json_for_usage_error():
    script = Path(__file__).parents[1] / "scripts" / "manage_postgres_backups.py"

    completed = subprocess.run(
        [sys.executable, str(script), "create-restore-point"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 2
    assert completed.stdout == b""
    payload = json.loads(completed.stderr.decode("utf-8"))
    assert payload["status"] == "failed"
    assert payload["error_code"] == "invalid_arguments"


def test_dump_failure_returns_exit_3_without_tool_output(tmp_path):
    runner = UnifiedFakeRunner()
    runner.fail_program = "pg_dump"
    code, output, error = _run(["create-daily"], tmp_path, runner=runner)

    assert code == 3
    assert output == ""
    assert json.loads(error)["error_code"] == "pg_dump_failed"
    assert "secret-output" not in error


def test_restore_failure_returns_exit_5_and_keeps_target_name(tmp_path):
    runner = UnifiedFakeRunner()
    storage = FakeStorage()
    _run(["create-daily"], tmp_path, runner=runner, storage=storage)
    manifest = next((tmp_path / "backups").glob("daily-*/manifest.json"))
    runner.fail_program = "pg_restore"

    code, output, error = _run(
        ["verify-restore", "--manifest", str(manifest), "--acknowledge-isolated"],
        tmp_path,
        runner=runner,
        storage=storage,
    )

    assert code == 5
    assert output == ""
    payload = json.loads(error)
    assert payload["status"] == "failed"
    assert payload["stage"] == "restore"
    assert re.fullmatch(r"backup_verify_[0-9a-f]{32}", payload["target_database"])
    assert "restore-secret" not in error
