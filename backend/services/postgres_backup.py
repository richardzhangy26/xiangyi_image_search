"""PostgreSQL 全量备份、恢复点和隔离恢复验证领域服务。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.backup_storage import BackupStorage, BackupStorageError


MANIFEST_SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 30
EXPECTED_POSTGRES_MAJOR = 16
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


class BackupError(RuntimeError):
    category = "integrity"

    def __init__(self, message: str, *, stage: str, error_code: str):
        super().__init__(message)
        self.stage = stage
        self.error_code = error_code


class BackupConfigError(BackupError):
    category = "config"

    def __init__(self, message: str, *, stage: str = "config", error_code: str = "invalid_config"):
        super().__init__(message, stage=stage, error_code=error_code)


class BackupIntegrityError(BackupError):
    category = "integrity"

    def __init__(self, message: str, *, stage: str = "integrity", error_code: str = "integrity_failed"):
        super().__init__(message, stage=stage, error_code=error_code)


class BackupConflictError(BackupIntegrityError):
    def __init__(self, message: str, *, stage: str = "reconcile"):
        super().__init__(message, stage=stage, error_code="backup_conflict")


class RestoreVerificationError(BackupError):
    category = "restore"

    def __init__(self, message: str, *, stage: str, error_code: str):
        super().__init__(message, stage=stage, error_code=error_code)


class RestoreSafetyError(RestoreVerificationError):
    def __init__(self, message: str, *, error_code: str = "restore_safety_gate"):
        super().__init__(message, stage="safety", error_code=error_code)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout: int,
    ) -> CommandResult:
        ...


class SubprocessCommandRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout: int,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                list(argv),
                env=dict(env),
                timeout=timeout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # 不传播可能带有命令输出的异常；调用方按非零状态映射到稳定阶段错误。
            return CommandResult(returncode=124, stdout=b"", stderr=b"")
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@dataclass(frozen=True)
class PostgresConnectionConfig:
    host: str
    port: int
    database: str
    user: str
    password: str

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
        *,
        prefix: str,
    ) -> "PostgresConnectionConfig":
        fields = ("HOST", "PORT", "NAME", "USER", "PASSWORD")
        missing = [f"{prefix}{field}" for field in fields if not environ.get(f"{prefix}{field}")]
        if missing:
            raise BackupConfigError(f"缺少专用数据库配置: {', '.join(missing)}")
        try:
            port = int(environ[f"{prefix}PORT"])
        except ValueError as exc:
            raise BackupConfigError(f"{prefix}PORT 必须是整数") from exc
        if not 1 <= port <= 65535:
            raise BackupConfigError(f"{prefix}PORT 超出有效范围")
        return cls(
            host=environ[f"{prefix}HOST"],
            port=port,
            database=environ[f"{prefix}NAME"],
            user=environ[f"{prefix}USER"],
            password=environ[f"{prefix}PASSWORD"],
        )

    @property
    def identity(self) -> Mapping[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "database": self.database,
        }

    def process_env(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update(
            {
                "PGHOST": self.host,
                "PGPORT": str(self.port),
                "PGDATABASE": self.database,
                "PGUSER": self.user,
                "PGPASSWORD": self.password,
            }
        )
        return environment


@dataclass(frozen=True)
class BackupRequest:
    backup_id: str
    kind: str
    purge_batch_id: Optional[str] = None

    @classmethod
    def daily(cls, on_date: Optional[date] = None) -> "BackupRequest":
        resolved = on_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
        return cls(backup_id=f"daily-{resolved.isoformat()}", kind="daily")

    @classmethod
    def daily_from_environment(
        cls,
        environ: Mapping[str, str],
        *,
        now: Optional[datetime] = None,
    ) -> "BackupRequest":
        timezone_name = environ.get("BACKUP_DAILY_TIMEZONE", "Asia/Shanghai")
        try:
            daily_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise BackupConfigError("BACKUP_DAILY_TIMEZONE 无效") from exc
        moment = now or datetime.now(timezone.utc)
        return cls.daily(moment.astimezone(daily_timezone).date())

    @classmethod
    def restore_point(cls, purge_batch_id: str) -> "BackupRequest":
        if not _IDENTIFIER.fullmatch(purge_batch_id):
            raise BackupConfigError("清除批次标识包含不安全字符")
        return cls(
            backup_id=f"purge-{purge_batch_id}",
            kind="purge_restore_point",
            purge_batch_id=purge_batch_id,
        )


@dataclass(frozen=True)
class BackupManifest:
    schema_version: int
    status: str
    backup_id: str
    kind: str
    purge_batch_id: Optional[str]
    created_at: datetime
    completed_at: datetime
    database_identity: Mapping[str, Any]
    postgres_client_major: int
    postgres_server_major: int
    artifact_file: str
    artifact_size: int
    artifact_sha256: str
    local_relative_path: str
    remote_bucket: str
    remote_dump_key: str
    remote_manifest_key: str
    retention_days: int
    retain_until: datetime
    verification: Mapping[str, Any]
    production_gates: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "backup_id": self.backup_id,
            "kind": self.kind,
            "purge_batch_id": self.purge_batch_id,
            "created_at": _iso(self.created_at),
            "completed_at": _iso(self.completed_at),
            "database_identity": dict(self.database_identity),
            "postgres": {
                "client_major": self.postgres_client_major,
                "server_major": self.postgres_server_major,
            },
            "artifact": {
                "file": self.artifact_file,
                "format": "postgresql-custom",
                "size_bytes": self.artifact_size,
                "sha256": self.artifact_sha256,
            },
            "copies": {
                "local": {"relative_path": self.local_relative_path},
                "remote": {
                    "bucket": self.remote_bucket,
                    "dump_key": self.remote_dump_key,
                    "manifest_key": self.remote_manifest_key,
                },
            },
            "retention": {
                "days": self.retention_days,
                "retain_until": _iso(self.retain_until),
            },
            "verification": dict(self.verification),
            "production_gates": dict(self.production_gates),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BackupManifest":
        try:
            _require_exact_keys(
                payload,
                {
                    "schema_version",
                    "status",
                    "backup_id",
                    "kind",
                    "purge_batch_id",
                    "created_at",
                    "completed_at",
                    "database_identity",
                    "postgres",
                    "artifact",
                    "copies",
                    "retention",
                    "verification",
                    "production_gates",
                },
            )
            if payload["schema_version"] != MANIFEST_SCHEMA_VERSION:
                raise ValueError("unknown schema")
            if payload["status"] != "complete":
                raise ValueError("not complete")
            artifact = payload["artifact"]
            copies = payload["copies"]
            retention = payload["retention"]
            postgres = payload["postgres"]
            _require_exact_keys(
                payload["database_identity"],
                {"host", "port", "database", "system_identifier"},
            )
            _require_exact_keys(postgres, {"client_major", "server_major"})
            _require_exact_keys(
                artifact,
                {"file", "format", "size_bytes", "sha256"},
            )
            if artifact["format"] != "postgresql-custom":
                raise ValueError("unknown artifact format")
            _require_exact_keys(copies, {"local", "remote"})
            _require_exact_keys(copies["local"], {"relative_path"})
            _require_exact_keys(
                copies["remote"],
                {"bucket", "dump_key", "manifest_key"},
            )
            _require_exact_keys(retention, {"days", "retain_until"})
            manifest = cls(
                schema_version=payload["schema_version"],
                status=payload["status"],
                backup_id=payload["backup_id"],
                kind=payload["kind"],
                purge_batch_id=payload.get("purge_batch_id"),
                created_at=_parse_datetime(payload["created_at"]),
                completed_at=_parse_datetime(payload["completed_at"]),
                database_identity=dict(payload["database_identity"]),
                postgres_client_major=int(postgres["client_major"]),
                postgres_server_major=int(postgres["server_major"]),
                artifact_file=artifact["file"],
                artifact_size=int(artifact["size_bytes"]),
                artifact_sha256=artifact["sha256"],
                local_relative_path=copies["local"]["relative_path"],
                remote_bucket=copies["remote"]["bucket"],
                remote_dump_key=copies["remote"]["dump_key"],
                remote_manifest_key=copies["remote"]["manifest_key"],
                retention_days=int(retention["days"]),
                retain_until=_parse_datetime(retention["retain_until"]),
                verification=dict(payload["verification"]),
                production_gates=dict(payload["production_gates"]),
            )
            validate_manifest_contract(manifest)
            return manifest
        except (KeyError, TypeError, ValueError) as exc:
            raise BackupIntegrityError(
                "manifest schema/status 无效",
                stage="manifest_validate",
                error_code="invalid_manifest",
            ) from exc


def _require_exact_keys(payload: Mapping[str, Any], expected: set[str]) -> None:
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise ValueError("unexpected manifest fields")


def validate_manifest_contract(
    manifest: BackupManifest,
    *,
    expected_remote_bucket: Optional[str] = None,
    expected_remote_prefix: Optional[str] = None,
) -> None:
    """集中验证不可变 manifest 身份、路径、哈希和版本契约。"""
    if manifest.schema_version != MANIFEST_SCHEMA_VERSION or manifest.status != "complete":
        raise BackupIntegrityError(
            "manifest schema/status 无效",
            stage="manifest_validate",
            error_code="invalid_manifest",
        )
    if not _IDENTIFIER.fullmatch(manifest.backup_id):
        raise BackupIntegrityError(
            "manifest 备份标识无效",
            stage="manifest_validate",
            error_code="invalid_backup_id",
        )
    if manifest.kind == "daily":
        if manifest.purge_batch_id is not None or not re.fullmatch(
            r"daily-\d{4}-\d{2}-\d{2}", manifest.backup_id
        ):
            raise BackupIntegrityError(
                "manifest 每日备份绑定无效",
                stage="manifest_validate",
                error_code="invalid_daily_binding",
            )
        try:
            date.fromisoformat(manifest.backup_id.removeprefix("daily-"))
        except ValueError as exc:
            raise BackupIntegrityError(
                "manifest 每日备份日期无效",
                stage="manifest_validate",
                error_code="invalid_daily_date",
            ) from exc
    elif manifest.kind == "purge_restore_point":
        if (
            not manifest.purge_batch_id
            or not _IDENTIFIER.fullmatch(manifest.purge_batch_id)
            or manifest.backup_id != f"purge-{manifest.purge_batch_id}"
        ):
            raise BackupIntegrityError(
                "manifest 清除批次绑定无效",
                stage="manifest_validate",
                error_code="invalid_batch_binding",
            )
    else:
        raise BackupIntegrityError(
            "manifest 备份类型无效",
            stage="manifest_validate",
            error_code="invalid_backup_kind",
        )

    identity = manifest.database_identity
    if (
        not isinstance(identity.get("host"), str)
        or not identity["host"]
        or not isinstance(identity.get("port"), int)
        or not 1 <= identity["port"] <= 65535
        or not isinstance(identity.get("database"), str)
        or not identity["database"]
        or not isinstance(identity.get("system_identifier"), str)
        or not identity["system_identifier"]
        or any(
            forbidden in str(key).lower()
            for key in identity
            for forbidden in ("password", "secret", "dsn", "token")
        )
    ):
        raise BackupIntegrityError(
            "manifest 数据库身份无效",
            stage="manifest_validate",
            error_code="invalid_database_identity",
        )
    if (
        manifest.postgres_client_major != EXPECTED_POSTGRES_MAJOR
        or manifest.postgres_server_major != EXPECTED_POSTGRES_MAJOR
    ):
        raise BackupIntegrityError(
            "manifest PostgreSQL major 不是 16",
            stage="manifest_validate",
            error_code="invalid_postgres_major",
        )
    if (
        manifest.artifact_file != "backup.dump"
        or manifest.artifact_size <= 0
        or not re.fullmatch(r"[0-9a-f]{64}", manifest.artifact_sha256)
    ):
        raise BackupIntegrityError(
            "manifest 备份工件无效",
            stage="manifest_validate",
            error_code="invalid_artifact",
        )
    if manifest.local_relative_path != f"{manifest.backup_id}/backup.dump":
        raise BackupIntegrityError(
            "manifest 本机路径绑定无效",
            stage="manifest_validate",
            error_code="invalid_local_path",
        )
    if not manifest.remote_bucket or (
        expected_remote_bucket is not None
        and manifest.remote_bucket != expected_remote_bucket
    ):
        raise BackupIntegrityError(
            "manifest 备份 Bucket 身份不匹配",
            stage="manifest_validate",
            error_code="remote_bucket_mismatch",
        )

    dump_suffix = f"/{manifest.backup_id}/backup.dump"
    if not manifest.remote_dump_key.endswith(dump_suffix):
        raise BackupIntegrityError(
            "manifest 异机对象键绑定无效",
            stage="manifest_validate",
            error_code="invalid_remote_key",
        )
    actual_prefix = manifest.remote_dump_key[: -len(dump_suffix)]
    try:
        _validate_remote_prefix(actual_prefix)
    except BackupConfigError as exc:
        raise BackupIntegrityError(
            "manifest 异机对象键前缀无效",
            stage="manifest_validate",
            error_code="invalid_remote_key",
        ) from exc
    if manifest.remote_manifest_key != (
        f"{actual_prefix}/{manifest.backup_id}/manifest.json"
    ) or (
        expected_remote_prefix is not None
        and actual_prefix != expected_remote_prefix.strip("/")
    ):
        raise BackupIntegrityError(
            "manifest 异机对象键绑定无效",
            stage="manifest_validate",
            error_code="invalid_remote_key",
        )
    if (
        manifest.retention_days != DEFAULT_RETENTION_DAYS
        or manifest.completed_at < manifest.created_at
        or manifest.retain_until
        != manifest.created_at + timedelta(days=DEFAULT_RETENTION_DAYS)
    ):
        raise BackupIntegrityError(
            "manifest 保留期限或时间顺序无效",
            stage="manifest_validate",
            error_code="invalid_retention",
        )
    expected_verification = {
        "local_pg_restore_list": "passed",
        "remote_dump_sha256": "passed",
        "remote_manifest_readback": "passed",
    }
    if dict(manifest.verification) != expected_verification:
        raise BackupIntegrityError(
            "manifest 完整性证据不完整",
            stage="manifest_validate",
            error_code="invalid_verification_evidence",
        )
    expected_gates = {
        "bucket_private": "not_verified",
        "server_side_encryption": "not_verified",
        "credential_least_privilege_no_delete": "not_verified",
        "retention_policy_30_days": "not_verified",
        "remote_restore_drill": "not_verified",
    }
    if dict(manifest.production_gates) != expected_gates:
        raise BackupIntegrityError(
            "manifest production gate 证据无效",
            stage="manifest_validate",
            error_code="invalid_production_gates",
        )


@dataclass(frozen=True)
class BackupResult:
    status: str
    manifest: BackupManifest

    def to_dict(self) -> Mapping[str, Any]:
        return {"status": self.status, "manifest": self.manifest.to_dict()}


@dataclass(frozen=True)
class CopyVerificationResult:
    status: str
    backup_id: str
    local_sha256: str
    remote_sha256: str

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "status": self.status,
            "backup_id": self.backup_id,
            "local_sha256": self.local_sha256,
            "remote_sha256": self.remote_sha256,
        }


class PostgresBackupService:
    def __init__(
        self,
        *,
        runner: CommandRunner,
        storage: BackupStorage,
        source: PostgresConnectionConfig,
        backup_root: Path,
        remote_bucket: str,
        remote_prefix: str,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        command_timeout: int = 3600,
        expected_postgres_major: int = EXPECTED_POSTGRES_MAJOR,
    ):
        if not remote_bucket:
            raise BackupConfigError("备份 Bucket 不能为空")
        _validate_remote_prefix(remote_prefix)
        self.runner = runner
        self.storage = storage
        self.source = source
        self.backup_root = Path(backup_root)
        self.remote_bucket = remote_bucket
        self.remote_prefix = remote_prefix.strip("/")
        self.now = now
        self.command_timeout = command_timeout
        self.expected_postgres_major = expected_postgres_major

    def create_backup(self, request: BackupRequest) -> BackupResult:
        _validate_request(request)
        self._ensure_root()
        final_directory = self.backup_root / request.backup_id
        manifest_path = final_directory / "manifest.json"
        if manifest_path.exists():
            manifest = self._read_manifest(manifest_path)
            _, _, database_identity = self._validate_postgres()
            self._validate_binding(manifest, request, database_identity)
            self.verify_copies(manifest_path)
            return BackupResult(status="complete", manifest=manifest)

        try:
            client_major, server_major, database_identity = self._validate_postgres()
            artifact, created_at = self._create_or_reconcile_local(
                request,
                final_directory,
                client_major=client_major,
                server_major=server_major,
                database_identity=database_identity,
            )
            remote_dump_key = self._remote_key(request.backup_id, "backup.dump")
            remote_manifest_key = self._remote_key(request.backup_id, "manifest.json")
            self._store_and_verify_remote_dump(
                remote_dump_key,
                artifact["path"],
                expected_size=artifact["size"],
                expected_sha256=artifact["sha256"],
                backup_id=request.backup_id,
            )
            candidate_path = final_directory / "candidate-manifest.json"
            if candidate_path.exists():
                manifest_bytes = candidate_path.read_bytes()
                manifest = BackupManifest.from_dict(json.loads(manifest_bytes))
                self._validate_binding(manifest, request, database_identity)
                if (
                    manifest.artifact_size != artifact["size"]
                    or manifest.artifact_sha256 != artifact["sha256"]
                ):
                    raise BackupConflictError("candidate manifest 与本机工件不一致")
            else:
                completed_at = _as_utc(self.now())
                manifest = BackupManifest(
                    schema_version=MANIFEST_SCHEMA_VERSION,
                    status="complete",
                    backup_id=request.backup_id,
                    kind=request.kind,
                    purge_batch_id=request.purge_batch_id,
                    created_at=created_at,
                    completed_at=completed_at,
                    database_identity=database_identity,
                    postgres_client_major=client_major,
                    postgres_server_major=server_major,
                    artifact_file="backup.dump",
                    artifact_size=artifact["size"],
                    artifact_sha256=artifact["sha256"],
                    local_relative_path=f"{request.backup_id}/backup.dump",
                    remote_bucket=self.remote_bucket,
                    remote_dump_key=remote_dump_key,
                    remote_manifest_key=remote_manifest_key,
                    retention_days=DEFAULT_RETENTION_DAYS,
                    retain_until=created_at + timedelta(days=DEFAULT_RETENTION_DAYS),
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
                validate_manifest_contract(
                    manifest,
                    expected_remote_bucket=self.remote_bucket,
                    expected_remote_prefix=self.remote_prefix,
                )
                manifest_bytes = _json_bytes(manifest.to_dict())
                _write_bytes_atomic(candidate_path, manifest_bytes, mode=0o600)
            self._store_and_verify_remote_manifest(
                remote_manifest_key,
                manifest_bytes,
                backup_id=request.backup_id,
            )
            os.replace(candidate_path, manifest_path)
            _fsync_directory(final_directory)
            self._write_attempt(
                final_directory,
                {
                    "status": "complete",
                    "stage": "complete",
                    "backup_id": request.backup_id,
                },
            )
            return BackupResult(status="complete", manifest=manifest)
        except Exception as exc:
            if final_directory.exists():
                stage = getattr(exc, "stage", "storage" if isinstance(exc, BackupStorageError) else "backup")
                error_code = getattr(exc, "error_code", type(exc).__name__)
                attempt_path = final_directory / "attempt-result.json"
                previous_attempt = (
                    dict(_read_json(attempt_path)) if attempt_path.exists() else {}
                )
                self._write_attempt(
                    final_directory,
                    {
                        **previous_attempt,
                        "status": "failed",
                        "stage": stage,
                        "error_code": error_code,
                        "backup_id": request.backup_id,
                        "local_artifact": (
                            f"{request.backup_id}/backup.dump"
                            if (final_directory / "backup.dump").exists()
                            else None
                        ),
                        "remote_dump_key": self._remote_key(request.backup_id, "backup.dump"),
                    },
                )
            raise

    def verify_copies(self, manifest_path: Path) -> CopyVerificationResult:
        manifest_path = Path(manifest_path)
        manifest = self._read_manifest(manifest_path)
        validate_manifest_contract(
            manifest,
            expected_remote_bucket=self.remote_bucket,
            expected_remote_prefix=self.remote_prefix,
        )
        local_dump = self.backup_root / manifest.local_relative_path
        _assert_within(local_dump, self.backup_root)
        if not local_dump.is_file():
            raise BackupIntegrityError("manifest 指向的本机备份不存在", stage="local_verify")
        local_sha256 = _sha256_file(local_dump)
        if local_sha256 != manifest.artifact_sha256:
            raise BackupIntegrityError("本机备份哈希不匹配", stage="local_verify")
        remote_sha256 = self._download_sha256(manifest.remote_dump_key)
        if remote_sha256 != manifest.artifact_sha256:
            raise BackupIntegrityError("异机备份哈希不匹配", stage="remote_dump_verify")
        with tempfile.TemporaryFile(mode="w+b") as target:
            self.storage.download_to(manifest.remote_manifest_key, target)
            target.seek(0)
            if target.read() != manifest_path.read_bytes():
                raise BackupIntegrityError(
                    "异机 final manifest 与本机提交标记不一致",
                    stage="remote_manifest_verify",
                    error_code="remote_manifest_mismatch",
                )
        return CopyVerificationResult(
            status="verified",
            backup_id=manifest.backup_id,
            local_sha256=local_sha256,
            remote_sha256=remote_sha256,
        )

    def _validate_postgres(self) -> tuple[int, int, Mapping[str, Any]]:
        environment = self.source.process_env()
        majors = []
        for program in ("pg_dump", "pg_restore", "psql", "createdb"):
            result = self.runner.run(
                [program, "--version"],
                env=environment,
                timeout=30,
            )
            if result.returncode != 0:
                raise BackupConfigError(
                    f"缺少可执行的 PostgreSQL client: {program}",
                    stage="client_preflight",
                    error_code="postgres_client_unavailable",
                )
            match = re.search(rb"(?:PostgreSQL\)?\s+)(\d+)(?:\.|\b)", result.stdout)
            if not match:
                raise BackupConfigError(
                    f"无法识别 PostgreSQL client 版本: {program}",
                    stage="client_preflight",
                    error_code="postgres_client_version_unknown",
                )
            majors.append(int(match.group(1)))
        if set(majors) != {self.expected_postgres_major}:
            raise BackupConfigError(
                f"PostgreSQL client major 必须全部为 {self.expected_postgres_major}",
                stage="client_preflight",
                error_code="postgres_client_major_mismatch",
            )

        identity_result = self.runner.run(
            [
                "psql",
                "--tuples-only",
                "--no-align",
                "--set=ON_ERROR_STOP=1",
                "--command=SELECT current_database() || '|' || current_setting('server_version_num') || '|' || system_identifier::text FROM pg_control_system()",
            ],
            env=environment,
            timeout=30,
        )
        if identity_result.returncode != 0:
            raise BackupIntegrityError(
                "无法读取源数据库身份",
                stage="database_identity",
                error_code="database_identity_failed",
            )
        try:
            database_name, server_version_num, system_identifier = (
                identity_result.stdout.decode("utf-8").strip().split("|", 2)
            )
            server_major = int(server_version_num) // 10000
        except (UnicodeDecodeError, ValueError) as exc:
            raise BackupIntegrityError(
                "源数据库身份响应无效",
                stage="database_identity",
                error_code="database_identity_invalid",
            ) from exc
        if database_name != self.source.database:
            raise BackupConflictError("配置与实际数据库身份不匹配", stage="database_identity")
        if server_major != self.expected_postgres_major:
            raise BackupConfigError(
                f"源 PostgreSQL server major 必须为 {self.expected_postgres_major}",
                stage="client_preflight",
                error_code="postgres_server_major_mismatch",
            )
        return majors[0], server_major, {
            **self.source.identity,
            "system_identifier": system_identifier,
        }

    def _create_or_reconcile_local(
        self,
        request: BackupRequest,
        final_directory: Path,
        *,
        client_major: int,
        server_major: int,
        database_identity: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], datetime]:
        dump_path = final_directory / "backup.dump"
        attempt_path = final_directory / "attempt-result.json"
        if dump_path.exists():
            attempt = _read_json(attempt_path)
            if attempt.get("database_identity") != dict(database_identity):
                raise BackupConflictError("同一备份标识绑定了不同数据库身份")
            if attempt.get("kind") != request.kind or attempt.get("purge_batch_id") != request.purge_batch_id:
                raise BackupConflictError("同一备份标识绑定了不同备份请求")
            created_at = _parse_datetime(attempt["created_at"])
            expected_artifact = attempt.get("artifact") or {}
            current_size = dump_path.stat().st_size
            current_sha256 = _sha256_file(dump_path)
            if (
                expected_artifact.get("size_bytes") != current_size
                or expected_artifact.get("sha256") != current_sha256
            ):
                raise BackupConflictError("本机 partial 备份工件已被修改")
            self._validate_dump(dump_path)
            return {
                "path": dump_path,
                "size": current_size,
                "sha256": current_sha256,
            }, created_at

        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{request.backup_id}-",
                dir=str(self.backup_root),
            )
        )
        os.chmod(staging, 0o700)
        staged_dump = staging / "backup.dump"
        created_at = _as_utc(self.now())
        try:
            result = self.runner.run(
                [
                    "pg_dump",
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    f"--file={staged_dump}",
                ],
                env=self.source.process_env(),
                timeout=self.command_timeout,
            )
            if result.returncode != 0:
                raise BackupIntegrityError(
                    "pg_dump 执行失败",
                    stage="dump",
                    error_code="pg_dump_failed",
                )
            if not staged_dump.is_file() or staged_dump.stat().st_size <= 0:
                raise BackupIntegrityError(
                    "pg_dump 未生成有效文件",
                    stage="dump",
                    error_code="empty_dump",
                )
            os.chmod(staged_dump, 0o600)
            _fsync_file(staged_dump)
            self._validate_dump(staged_dump)
            artifact = {
                "path": staged_dump,
                "size": staged_dump.stat().st_size,
                "sha256": _sha256_file(staged_dump),
            }
            _write_json_atomic(
                staging / "attempt-result.json",
                {
                    "status": "partial",
                    "stage": "local_complete",
                    "backup_id": request.backup_id,
                    "kind": request.kind,
                    "purge_batch_id": request.purge_batch_id,
                    "created_at": _iso(created_at),
                    "database_identity": dict(database_identity),
                    "postgres": {
                        "client_major": client_major,
                        "server_major": server_major,
                    },
                    "artifact": {
                        "file": "backup.dump",
                        "size_bytes": artifact["size"],
                        "sha256": artifact["sha256"],
                    },
                },
            )
            if final_directory.exists():
                raise BackupConflictError("备份目录已被并发创建")
            os.replace(staging, final_directory)
            _fsync_directory(self.backup_root)
            return {
                **artifact,
                "path": final_directory / "backup.dump",
            }, created_at
        except Exception:
            if staging.exists():
                for path in staging.iterdir():
                    path.unlink()
                staging.rmdir()
            raise

    def _validate_dump(self, dump_path: Path) -> None:
        result = self.runner.run(
            ["pg_restore", "--list", str(dump_path)],
            env=self.source.process_env(),
            timeout=self.command_timeout,
        )
        if result.returncode != 0:
            raise BackupIntegrityError(
                "pg_restore 无法读取备份目录",
                stage="local_dump_verify",
                error_code="pg_restore_list_failed",
            )

    def _store_and_verify_remote_dump(
        self,
        key: str,
        path: Path,
        *,
        expected_size: int,
        expected_sha256: str,
        backup_id: str,
    ) -> None:
        existing = self.storage.head(key)
        if existing is None:
            self.storage.put_file_if_absent(
                key,
                path,
                metadata={
                    "backup-id": backup_id,
                    "sha256": expected_sha256,
                    "retention-days": str(DEFAULT_RETENTION_DAYS),
                },
            )
        elif (
            existing.size != expected_size
            or existing.metadata.get("sha256") != expected_sha256
            or existing.metadata.get("backup-id") != backup_id
        ):
            raise BackupConflictError("异机备份对象与本机工件不一致", stage="remote_dump_reconcile")
        actual_sha256 = self._download_sha256(key)
        if actual_sha256 != expected_sha256:
            raise BackupIntegrityError(
                "异机备份哈希不匹配",
                stage="remote_dump_verify",
                error_code="remote_dump_hash_mismatch",
            )

    def _store_and_verify_remote_manifest(
        self,
        key: str,
        data: bytes,
        *,
        backup_id: str,
    ) -> None:
        digest = hashlib.sha256(data).hexdigest()
        existing = self.storage.head(key)
        if existing is None:
            self.storage.put_bytes_if_absent(
                key,
                data,
                metadata={"backup-id": backup_id, "sha256": digest},
            )
        elif existing.size != len(data) or existing.metadata.get("sha256") != digest:
            raise BackupConflictError("异机 final manifest 冲突", stage="remote_manifest_reconcile")
        with tempfile.TemporaryFile(mode="w+b") as target:
            self.storage.download_to(key, target)
            target.seek(0)
            if target.read() != data:
                raise BackupIntegrityError(
                    "异机 final manifest 读回不一致",
                    stage="remote_manifest_verify",
                    error_code="remote_manifest_mismatch",
                )

    def _download_sha256(self, key: str) -> str:
        digest = hashlib.sha256()
        with tempfile.TemporaryFile(mode="w+b") as target:
            self.storage.download_to(key, target)
            target.seek(0)
            while True:
                chunk = target.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _read_manifest(self, path: Path) -> BackupManifest:
        return BackupManifest.from_dict(_read_json(path))

    def _validate_binding(
        self,
        manifest: BackupManifest,
        request: BackupRequest,
        database_identity: Mapping[str, Any],
    ) -> None:
        validate_manifest_contract(
            manifest,
            expected_remote_bucket=self.remote_bucket,
            expected_remote_prefix=self.remote_prefix,
        )
        if manifest.backup_id != request.backup_id:
            raise BackupConflictError("manifest 备份标识不匹配")
        if manifest.kind != request.kind or manifest.purge_batch_id != request.purge_batch_id:
            raise BackupConflictError("manifest 清除批次绑定不匹配")
        if dict(manifest.database_identity) != dict(database_identity):
            raise BackupConflictError("manifest 数据库身份不匹配")

    def _remote_key(self, backup_id: str, file_name: str) -> str:
        return f"{self.remote_prefix}/{backup_id}/{file_name}"

    def _ensure_root(self) -> None:
        self.backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.backup_root, 0o700)

    def _write_attempt(self, directory: Path, payload: Mapping[str, Any]) -> None:
        _write_json_atomic(directory / "attempt-result.json", payload)


@dataclass(frozen=True)
class RestoreVerificationConfig:
    host: str
    port: int
    maintenance_database: str
    user: str
    password: str
    disposable: bool

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "RestoreVerificationConfig":
        names = (
            "RESTORE_VERIFY_DB_HOST",
            "RESTORE_VERIFY_DB_PORT",
            "RESTORE_VERIFY_DB_NAME",
            "RESTORE_VERIFY_DB_USER",
            "RESTORE_VERIFY_DB_PASSWORD",
        )
        missing = [name for name in names if not environ.get(name)]
        if missing:
            raise BackupConfigError(
                f"缺少隔离恢复数据库配置: {', '.join(missing)}"
            )
        try:
            port = int(environ["RESTORE_VERIFY_DB_PORT"])
        except ValueError as exc:
            raise BackupConfigError("RESTORE_VERIFY_DB_PORT 必须是整数") from exc
        return cls(
            host=environ["RESTORE_VERIFY_DB_HOST"],
            port=port,
            maintenance_database=environ["RESTORE_VERIFY_DB_NAME"],
            user=environ["RESTORE_VERIFY_DB_USER"],
            password=environ["RESTORE_VERIFY_DB_PASSWORD"],
            disposable=environ.get("RESTORE_VERIFY_DISPOSABLE") == "1",
        )

    def process_env(self, *, database: Optional[str] = None) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update(
            {
                "PGHOST": self.host,
                "PGPORT": str(self.port),
                "PGDATABASE": database or self.maintenance_database,
                "PGUSER": self.user,
                "PGPASSWORD": self.password,
            }
        )
        return environment


@dataclass(frozen=True)
class RestoreVerificationResult:
    status: str
    stage: str
    error_code: Optional[str]
    target_database: str
    evidence: Mapping[str, Any]

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "status": self.status,
            "stage": self.stage,
            "error_code": self.error_code,
            "target_database": self.target_database,
            "evidence": dict(self.evidence),
        }


class PostgresRestoreVerifier:
    """只在显式 disposable PostgreSQL 中创建新数据库并验证恢复。"""

    def __init__(
        self,
        *,
        runner: CommandRunner,
        storage: BackupStorage,
        config: RestoreVerificationConfig,
        temporary_root: Path,
        remote_bucket: str,
        remote_prefix: str,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        command_timeout: int = 3600,
    ):
        self.runner = runner
        self.storage = storage
        self.config = config
        self.temporary_root = Path(temporary_root)
        self.remote_bucket = remote_bucket
        self.remote_prefix = remote_prefix.strip("/")
        _validate_remote_prefix(self.remote_prefix)
        self.uuid_factory = uuid_factory
        self.command_timeout = command_timeout

    def verify_from_remote(
        self,
        manifest: BackupManifest,
        *,
        acknowledge_isolated: bool,
    ) -> RestoreVerificationResult:
        self._validate_manifest(manifest)
        if not acknowledge_isolated:
            raise RestoreSafetyError("必须显式确认只在隔离数据库执行恢复")
        if not self.config.disposable:
            raise RestoreSafetyError("RESTORE_VERIFY_DISPOSABLE 必须为 1")
        source_host = manifest.database_identity.get("host")
        source_port = int(manifest.database_identity.get("port", 0))
        if self.config.host == source_host and self.config.port == source_port:
            raise RestoreSafetyError("隔离恢复实例不得与源数据库使用同一地址")

        self._validate_remote_manifest(manifest)
        self._validate_clients()
        restore_identity = self._read_restore_identity()
        if restore_identity["system_identifier"] == manifest.database_identity.get(
            "system_identifier"
        ):
            raise RestoreSafetyError("隔离恢复实例不得与源数据库具有同一系统身份")

        self.temporary_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.temporary_root, 0o700)
        temporary_directory = Path(
            tempfile.mkdtemp(prefix="restore-verify-", dir=str(self.temporary_root))
        )
        os.chmod(temporary_directory, 0o700)
        dump_path = temporary_directory / "backup.dump"
        target_database = f"backup_verify_{self.uuid_factory().hex}"
        try:
            with dump_path.open("xb") as stream:
                os.chmod(dump_path, 0o600)
                self.storage.download_to(manifest.remote_dump_key, stream)
                stream.flush()
                os.fsync(stream.fileno())
            if dump_path.stat().st_size != manifest.artifact_size:
                raise BackupIntegrityError(
                    "异机备份大小不匹配",
                    stage="remote_dump_verify",
                    error_code="remote_dump_size_mismatch",
                )
            if _sha256_file(dump_path) != manifest.artifact_sha256:
                raise BackupIntegrityError(
                    "异机备份哈希不匹配",
                    stage="remote_dump_verify",
                    error_code="remote_dump_hash_mismatch",
                )

            self._assert_database_absent(target_database)
            create_result = self.runner.run(
                ["createdb", "--template=template0", target_database],
                env=self.config.process_env(),
                timeout=120,
            )
            if create_result.returncode != 0:
                return self._failed(
                    target_database,
                    stage="create_database",
                    error_code="createdb_failed",
                )
            restore_result = self.runner.run(
                [
                    "pg_restore",
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                    "--no-acl",
                    f"--dbname={target_database}",
                    str(dump_path),
                ],
                env=self.config.process_env(database=target_database),
                timeout=self.command_timeout,
            )
            if restore_result.returncode != 0:
                return self._failed(
                    target_database,
                    stage="restore",
                    error_code="pg_restore_failed",
                )
            evidence_result = self.runner.run(
                [
                    "psql",
                    "--tuples-only",
                    "--no-align",
                    "--set=ON_ERROR_STOP=1",
                    f"--dbname={target_database}",
                    f"--command={_STRUCTURE_EVIDENCE_SQL}",
                ],
                env=self.config.process_env(database=target_database),
                timeout=120,
            )
            if evidence_result.returncode != 0:
                return self._failed(
                    target_database,
                    stage="structure_verify",
                    error_code="structure_query_failed",
                )
            try:
                evidence = json.loads(evidence_result.stdout.decode("utf-8").strip())
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._failed(
                    target_database,
                    stage="structure_verify",
                    error_code="structure_evidence_invalid",
                )
            if not _structure_evidence_valid(evidence):
                return self._failed(
                    target_database,
                    stage="structure_verify",
                    error_code="structure_mismatch",
                    evidence=evidence,
                )
            return RestoreVerificationResult(
                status="verified",
                stage="complete",
                error_code=None,
                target_database=target_database,
                evidence=evidence,
            )
        finally:
            if dump_path.exists():
                dump_path.unlink()
            if temporary_directory.exists():
                temporary_directory.rmdir()

    def _validate_manifest(self, manifest: BackupManifest) -> None:
        validate_manifest_contract(
            manifest,
            expected_remote_bucket=self.remote_bucket,
            expected_remote_prefix=self.remote_prefix,
        )

    def _validate_remote_manifest(self, manifest: BackupManifest) -> None:
        expected = _json_bytes(manifest.to_dict())
        with tempfile.TemporaryFile(mode="w+b") as target:
            self.storage.download_to(manifest.remote_manifest_key, target)
            target.seek(0)
            if target.read() != expected:
                raise BackupIntegrityError(
                    "异机 final manifest 与恢复请求不一致",
                    stage="remote_manifest_verify",
                    error_code="remote_manifest_mismatch",
                )

    def _validate_clients(self) -> None:
        environment = self.config.process_env()
        for program in ("pg_restore", "psql", "createdb"):
            result = self.runner.run(
                [program, "--version"], env=environment, timeout=30
            )
            match = re.search(rb"(?:PostgreSQL\)?\s+)(\d+)(?:\.|\b)", result.stdout)
            if result.returncode != 0 or not match or int(match.group(1)) != EXPECTED_POSTGRES_MAJOR:
                raise RestoreSafetyError(
                    f"恢复环境必须提供 PostgreSQL {EXPECTED_POSTGRES_MAJOR} client",
                    error_code="restore_client_invalid",
                )

    def _read_restore_identity(self) -> Mapping[str, Any]:
        result = self.runner.run(
            [
                "psql",
                "--tuples-only",
                "--no-align",
                "--set=ON_ERROR_STOP=1",
                "--command=SELECT system_identifier::text || '|' || current_database() || '|' || current_setting('server_version_num') FROM pg_control_system()",
            ],
            env=self.config.process_env(),
            timeout=30,
        )
        if result.returncode != 0:
            raise RestoreSafetyError(
                "无法读取隔离恢复实例身份",
                error_code="restore_identity_failed",
            )
        try:
            system_identifier, database, version_num = result.stdout.decode(
                "utf-8"
            ).strip().split("|", 2)
            server_major = int(version_num) // 10000
        except (UnicodeDecodeError, ValueError) as exc:
            raise RestoreSafetyError(
                "隔离恢复实例身份响应无效",
                error_code="restore_identity_invalid",
            ) from exc
        if database != self.config.maintenance_database or server_major != EXPECTED_POSTGRES_MAJOR:
            raise RestoreSafetyError(
                "隔离恢复实例名称或版本不匹配",
                error_code="restore_identity_mismatch",
            )
        return {
            "system_identifier": system_identifier,
            "database": database,
            "server_major": server_major,
        }

    def _assert_database_absent(self, target_database: str) -> None:
        if not re.fullmatch(r"backup_verify_[0-9a-f]{32}", target_database):
            raise RestoreSafetyError("程序生成的目标数据库名无效")
        result = self.runner.run(
            [
                "psql",
                "--tuples-only",
                "--no-align",
                "--set=ON_ERROR_STOP=1",
                f"--command=SELECT 1 FROM pg_database WHERE datname = '{target_database}'",
            ],
            env=self.config.process_env(),
            timeout=30,
        )
        if result.returncode != 0:
            raise RestoreSafetyError(
                "无法确认目标数据库不存在",
                error_code="target_absence_check_failed",
            )
        if result.stdout.strip():
            raise RestoreSafetyError(
                "程序生成的目标数据库已存在，拒绝覆盖",
                error_code="target_database_exists",
            )

    @staticmethod
    def _failed(
        target_database: str,
        *,
        stage: str,
        error_code: str,
        evidence: Optional[Mapping[str, Any]] = None,
    ) -> RestoreVerificationResult:
        return RestoreVerificationResult(
            status="failed",
            stage=stage,
            error_code=error_code,
            target_database=target_database,
            evidence=dict(evidence or {}),
        )


_STRUCTURE_EVIDENCE_SQL = """SELECT json_build_object(
  'vector_extension', EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector'),
  'products_table', to_regclass('public.products') IS NOT NULL,
  'image_assets_table', to_regclass('public.image_assets') IS NOT NULL,
  'vector_type', COALESCE((
      SELECT format_type(a.atttypid, a.atttypmod)
      FROM pg_attribute a
      JOIN pg_class c ON c.oid = a.attrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relname = 'image_assets'
        AND a.attname = 'vector' AND NOT a.attisdropped
  ), ''),
  'products_count', (SELECT count(*) FROM public.products),
  'image_assets_count', (SELECT count(*) FROM public.image_assets)
)"""


def _structure_evidence_valid(evidence: Mapping[str, Any]) -> bool:
    return (
        evidence.get("vector_extension") is True
        and evidence.get("products_table") is True
        and evidence.get("image_assets_table") is True
        and evidence.get("vector_type") == "vector(1024)"
        and isinstance(evidence.get("products_count"), int)
        and evidence["products_count"] >= 0
        and isinstance(evidence.get("image_assets_count"), int)
        and evidence["image_assets_count"] >= 0
    )


def _validate_request(request: BackupRequest) -> None:
    if not _IDENTIFIER.fullmatch(request.backup_id):
        raise BackupConfigError("备份标识包含不安全字符")
    if request.kind not in {"daily", "purge_restore_point"}:
        raise BackupConfigError("备份类型无效")
    if request.kind == "purge_restore_point" and not request.purge_batch_id:
        raise BackupConfigError("即时恢复点缺少清除批次标识")
    if request.kind == "daily" and request.purge_batch_id is not None:
        raise BackupConfigError("每日备份不得绑定清除批次")


def _validate_remote_prefix(prefix: str) -> None:
    normalized = prefix.strip("/")
    if (
        not normalized
        or "\\" in normalized
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise BackupConfigError("异机备份前缀不安全")


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError(
            "manifest 或 attempt JSON 不可读",
            stage="manifest_validate",
            error_code="invalid_json",
        ) from exc


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    _write_bytes_atomic(path, _json_bytes(payload), mode=0o600)


def _write_bytes_atomic(path: Path, data: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _assert_within(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise BackupIntegrityError(
            "manifest 本机路径越界",
            stage="manifest_validate",
            error_code="unsafe_local_path",
        ) from exc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise BackupConfigError("时间必须包含时区")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
