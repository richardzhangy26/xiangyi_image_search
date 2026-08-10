"""显式 disposable pgvector 环境中的真实恢复接缝。

本测试默认跳过。本仓普通测试绝不连接 PostgreSQL 或 OSS。
"""

import os
import re
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


REQUIRED_ENVIRONMENT = (
    "DISPOSABLE_SOURCE_ADMIN_DB_HOST",
    "DISPOSABLE_SOURCE_ADMIN_DB_PORT",
    "DISPOSABLE_SOURCE_ADMIN_DB_NAME",
    "DISPOSABLE_SOURCE_ADMIN_DB_USER",
    "DISPOSABLE_SOURCE_ADMIN_DB_PASSWORD",
    "BACKUP_DB_HOST",
    "BACKUP_DB_PORT",
    "BACKUP_DB_USER",
    "BACKUP_DB_PASSWORD",
    "RESTORE_VERIFY_DB_HOST",
    "RESTORE_VERIFY_DB_PORT",
    "RESTORE_VERIFY_DB_NAME",
    "RESTORE_VERIFY_DB_USER",
    "RESTORE_VERIFY_DB_PASSWORD",
)


def _require_explicit_disposable_gate():
    if os.environ.get("RUN_DISPOSABLE_BACKUP_RESTORE_TEST") != "1":
        pytest.skip("需要 RUN_DISPOSABLE_BACKUP_RESTORE_TEST=1 显式门")
    if os.environ.get("RESTORE_VERIFY_DISPOSABLE") != "1":
        pytest.skip("需要 RESTORE_VERIFY_DISPOSABLE=1 隔离实例声明")
    missing = [name for name in REQUIRED_ENVIRONMENT if not os.environ.get(name)]
    if missing:
        pytest.skip("缺少 disposable PostgreSQL 配置: " + ", ".join(missing))


class _MemoryBackupStorage:
    def __init__(self):
        self.objects = {}

    def head(self, key):
        item = self.objects.get(key)
        if item is None:
            return None
        data, metadata = item
        return SimpleNamespace(key=key, size=len(data), metadata=dict(metadata))

    def put_file_if_absent(self, key, path, *, metadata):
        if key in self.objects:
            raise AssertionError("integration fake storage 禁止覆盖")
        self.objects[key] = (Path(path).read_bytes(), dict(metadata))

    def put_bytes_if_absent(self, key, data, *, metadata):
        if key in self.objects:
            raise AssertionError("integration fake storage 禁止覆盖")
        self.objects[key] = (bytes(data), dict(metadata))

    def download_to(self, key, target):
        target.write(self.objects[key][0])


def test_custom_dump_restores_pgvector_schema_and_rows(tmp_path):
    _require_explicit_disposable_gate()
    from services.postgres_backup import (
        BackupRequest,
        PostgresBackupService,
        PostgresConnectionConfig,
        PostgresRestoreVerifier,
        RestoreVerificationConfig,
        SubprocessCommandRunner,
    )

    environment = dict(os.environ)
    storage = _MemoryBackupStorage()
    runner = SubprocessCommandRunner()
    restore = RestoreVerificationConfig.from_env(environment)
    source_database = f"backup_source_{uuid4().hex}"
    source_admin = PostgresConnectionConfig.from_env(
        environment,
        prefix="DISPOSABLE_SOURCE_ADMIN_DB_",
    )
    backup_host = environment["BACKUP_DB_HOST"]
    backup_port = int(environment["BACKUP_DB_PORT"])
    assert (source_admin.host, source_admin.port) == (backup_host, backup_port), (
        "disposable 源管理员与备份连接必须指向同一源实例"
    )
    assert (backup_host, backup_port) != (restore.host, restore.port), (
        "disposable 源与恢复实例地址必须不同"
    )
    source_identity = runner.run(
        [
            "psql",
            "--tuples-only",
            "--no-align",
            "--set=ON_ERROR_STOP=1",
            "--command=SELECT system_identifier::text FROM pg_control_system()",
        ],
        env=source_admin.process_env(),
        timeout=30,
    )
    restore_identity = runner.run(
        [
            "psql",
            "--tuples-only",
            "--no-align",
            "--set=ON_ERROR_STOP=1",
            "--command=SELECT system_identifier::text FROM pg_control_system()",
        ],
        env=restore.process_env(),
        timeout=30,
    )
    assert source_identity.returncode == restore_identity.returncode == 0, (
        "无法只读确认 disposable 实例身份"
    )
    assert source_identity.stdout.strip() != restore_identity.stdout.strip(), (
        "disposable 源与恢复实例系统身份必须不同"
    )

    backup_user = environment["BACKUP_DB_USER"]
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", backup_user), (
        "集成测试备份角色名必须是简单 PostgreSQL 标识符"
    )
    create_source = runner.run(
        [
            "createdb",
            "--template=template0",
            f"--owner={environment['BACKUP_DB_USER']}",
            source_database,
        ],
        env=source_admin.process_env(),
        timeout=120,
    )
    assert create_source.returncode == 0, "disposable 源数据库创建失败"

    zero_vector = "[" + ",".join(["0"] * 1024) + "]"
    one_vector = "[1," + ",".join(["0"] * 1023) + "]"
    schema_sql = f"""
CREATE EXTENSION vector;
CREATE TABLE public.products (
  model_number varchar(100) PRIMARY KEY
);
CREATE TABLE public.image_assets (
  id bigserial PRIMARY KEY,
  model_number varchar(100) NOT NULL REFERENCES public.products(model_number),
  vector vector(1024) NOT NULL
);
INSERT INTO public.products(model_number) VALUES ('T10-A'), ('T10-B');
INSERT INTO public.image_assets(model_number, vector) VALUES
  ('T10-A', '{zero_vector}'::vector),
  ('T10-A', '{one_vector}'::vector),
  ('T10-B', '{zero_vector}'::vector);
GRANT USAGE ON SCHEMA public TO "{backup_user}";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{backup_user}";
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO "{backup_user}";
"""
    seed_source = runner.run(
        [
            "psql",
            "--set=ON_ERROR_STOP=1",
            f"--dbname={source_database}",
            f"--command={schema_sql}",
        ],
        env=source_admin.process_env(database=source_database),
        timeout=120,
    )
    assert seed_source.returncode == 0, "disposable 源数据库建模失败"

    source_environment = dict(environment)
    source_environment["BACKUP_DB_NAME"] = source_database
    source = PostgresConnectionConfig.from_env(
        source_environment,
        prefix="BACKUP_DB_",
    )
    request = BackupRequest.restore_point(f"integration-{uuid4().hex}")
    service = PostgresBackupService(
        runner=runner,
        storage=storage,
        source=source,
        backup_root=tmp_path / "backups",
        remote_bucket="integration-memory-storage",
        remote_prefix="postgresql-backups",
    )
    manifest = service.create_backup(request).manifest
    verifier = PostgresRestoreVerifier(
        runner=runner,
        storage=storage,
        config=restore,
        temporary_root=tmp_path / "restore-temp",
        remote_bucket="integration-memory-storage",
        remote_prefix="postgresql-backups",
    )

    result = verifier.verify_from_remote(manifest, acknowledge_isolated=True)

    assert result.status == "verified"
    assert result.evidence["vector_type"] == "vector(1024)"
    assert result.evidence["products_count"] == 2
    assert result.evidence["image_assets_count"] == 3
