# PostgreSQL Backup and Restore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. For this delegated execution, implement inline because the caller already selected execution in the current isolated worktree.

**Goal:** 提供可重入的每日全量备份、清除批次即时恢复点、本机与独立私有 OSS 双副本校验，以及显式门控的隔离 PostgreSQL 恢复验证。

**Architecture:** 备份领域编排、专用 OSS 适配器、无 shell 子进程执行器和 CLI 分离；final manifest 是异机副本通过字节级校验后的唯一完成标记。恢复验证只从远端副本创建程序生成的新数据库，不接受已有目标库，也不自动删除。

**Tech Stack:** Python 3.9+、pytest、PostgreSQL 16 client CLI、pgvector PostgreSQL 16、Aliyun `oss2`、JSON manifest。

## Global Constraints

- 基线固定为 detached `088bb9f`，不创建或移动分支引用。
- 禁止真实 PostgreSQL/OSS 写入、真实备份桶或凭证配置、部署、删除和真实恢复演练。
- 本轮只运行 fake runner/fake storage 定向测试；disposable pgvector 集成测试必须显式配置，否则 skip。
- 不把备份挂到 Flask 启动、健康检查或 `docker-compose` 应用服务。
- `BACKUP_*` 和 `RESTORE_VERIFY_*` 不回退应用凭证；密码、DSN、secret 和签名 URL 不进入 argv、JSON、日志或 manifest。
- final manifest 仅在本机 dump 可读且异机 dump/manifest 校验成功后写入；failed/partial attempt 不得供永久清除放行。
- 真实私有桶、SSE、独立 IAM、无 Delete 权限、30 天生命周期/Object Lock 与真实恢复演练未验证时，production gate 固定为 `not_verified`。
- 用户明确禁止 commit；每个任务以定向测试和 `git diff --check` 取代提交步骤。

---

### Task 1: 专用备份存储适配器

**Files:**
- Create: `backend/services/backup_storage.py`
- Test: `backend/test/test_backup_storage.py`

**Interfaces:**
- Produces: `BackupObject`, `BackupStorage` protocol, `BackupStorageConfig`, `OssBackupStorage.from_env()`, `put_file_if_absent()`, `put_bytes_if_absent()`, `download_to()`。
- Consumes: `oss2` SDK；不依赖应用 `OssObjectStorage`。

- [ ] **Step 1: 写配置隔离和不可覆盖失败测试**

```python
def test_backup_storage_requires_dedicated_bucket_and_credentials():
    environment = backup_environment()
    environment["OSS_BUCKET_NAME"] = environment["BACKUP_OSS_BUCKET_NAME"]
    with pytest.raises(BackupStorageConfigError, match="必须独立"):
        BackupStorageConfig.from_env(environment)

def test_storage_has_no_delete_capability():
    assert not hasattr(OssBackupStorage, "delete_object")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest test/test_backup_storage.py -v`

Expected: collection/import failure because `services.backup_storage` does not exist.

- [ ] **Step 3: 实现最小协议和专用配置**

```python
@dataclass(frozen=True)
class BackupStorageConfig:
    access_key_id: str
    access_key_secret: str
    endpoint: str
    bucket_name: str
    base_prefix: str
    server_side_encryption: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "BackupStorageConfig":
        names = ("ACCESS_KEY_ID", "ACCESS_KEY_SECRET", "ENDPOINT", "BUCKET_NAME")
        missing = [f"BACKUP_OSS_{name}" for name in names
                   if not environ.get(f"BACKUP_OSS_{name}")]
        if missing:
            raise BackupStorageConfigError("缺少专用备份配置: " + ", ".join(missing))
        bucket = environ["BACKUP_OSS_BUCKET_NAME"]
        if bucket == environ.get("OSS_BUCKET_NAME"):
            raise BackupStorageConfigError("备份 Bucket 必须独立于正式图片 Bucket")
        return cls(
            access_key_id=environ["BACKUP_OSS_ACCESS_KEY_ID"],
            access_key_secret=environ["BACKUP_OSS_ACCESS_KEY_SECRET"],
            endpoint=environ["BACKUP_OSS_ENDPOINT"],
            bucket_name=bucket,
            base_prefix=environ.get("BACKUP_OSS_BASE_PREFIX", "postgresql-backups").strip("/"),
            server_side_encryption=environ.get("BACKUP_OSS_SSE", "AES256"),
        )

class BackupStorage(Protocol):
    def head(self, key: str) -> Optional[BackupObject]:
        raise NotImplementedError
    def put_file_if_absent(self, key: str, path: Path, *, metadata: Mapping[str, str]) -> None:
        raise NotImplementedError
    def put_bytes_if_absent(self, key: str, data: bytes, *, metadata: Mapping[str, str]) -> None:
        raise NotImplementedError
    def download_to(self, key: str, target: BinaryIO) -> None:
        raise NotImplementedError
```

`OssBackupStorage` 的 PUT headers 固定含 `x-oss-forbid-overwrite=true`、`x-oss-object-acl=private`、`x-oss-server-side-encryption=<AES256|KMS>`；异常只映射为不含 SDK 原文的稳定错误。

- [ ] **Step 4: 覆盖已存在对象、下载和错误脱敏**

```python
def test_put_file_forces_private_sse_and_forbid_overwrite(tmp_path, fake_bucket, config):
    source = tmp_path / "backup.dump"
    source.write_bytes(b"archive")
    OssBackupStorage(fake_bucket, config).put_file_if_absent(
        "postgresql-backups/daily-2026-08-06/backup.dump",
        source,
        metadata={"sha256": hashlib.sha256(b"archive").hexdigest()},
    )
    headers = fake_bucket.put_file_calls[0][2]
    assert headers["x-oss-forbid-overwrite"] == "true"
    assert headers["x-oss-object-acl"] == "private"
    assert headers["x-oss-server-side-encryption"] == "AES256"

def test_existing_object_is_reported_as_conflict(fake_bucket, config):
    fake_bucket.put_error = FakeOssError(status=409)
    with pytest.raises(BackupStorageConflictError):
        OssBackupStorage(fake_bucket, config).put_bytes_if_absent(
            "postgresql-backups/id/manifest.json", b"{}", metadata={}
        )

def test_download_streams_bytes_without_signed_url(fake_bucket, config):
    fake_bucket.download_bytes = b"archive"
    target = io.BytesIO()
    OssBackupStorage(fake_bucket, config).download_to("key", target)
    assert target.getvalue() == b"archive"
    assert fake_bucket.sign_calls == []

def test_sdk_error_does_not_leak_secret(fake_bucket, config):
    fake_bucket.head_error = RuntimeError("secret-value")
    with pytest.raises(BackupStorageError) as caught:
        OssBackupStorage(fake_bucket, config).head("key")
    assert "secret-value" not in str(caught.value)
```

- [ ] **Step 5: 运行定向测试和静态检查**

Run: `cd backend && python -m pytest test/test_backup_storage.py -v`

Expected: all tests PASS and no network call.

Run: `git diff --check`

Expected: no output.

### Task 2: Manifest-last 备份领域工作流

**Files:**
- Create: `backend/services/postgres_backup.py`
- Test: `backend/test/test_postgres_backup.py`

**Interfaces:**
- Consumes: Task 1 `BackupStorage`。
- Produces: `CommandRunner.run(argv, env, timeout)`, `PostgresConnectionConfig`, `BackupRequest`, `BackupManifest`, `BackupResult`, `PostgresBackupService.create_backup()`, `verify_copies()`。

- [ ] **Step 1: 写稳定 ID、配置隔离和命令安全测试**

```python
def test_restore_point_id_is_stably_bound_to_batch():
    request = BackupRequest.restore_point("purge-batch-001")
    assert request.backup_id == "purge-purge-batch-001"

def test_backup_config_does_not_fall_back_to_application_db_vars():
    with pytest.raises(BackupConfigError, match="BACKUP_DB_HOST"):
        PostgresConnectionConfig.from_env({"DB_HOST": "production"}, prefix="BACKUP_DB_")

def test_pg_password_is_only_in_explicit_subprocess_environment(backup_service, runner):
    service.create_backup(request)
    assert all("secret" not in " ".join(call.argv) for call in runner.calls)
    assert runner.calls[0].env["PGPASSWORD"] == "secret"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest test/test_postgres_backup.py -v`

Expected: import failure because `services.postgres_backup` does not exist.

- [ ] **Step 3: 实现领域类型、标识校验和 runner**

```python
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

class SubprocessCommandRunner:
    def run(self, argv: Sequence[str], *, env: Mapping[str, str], timeout: int) -> CommandResult:
        completed = subprocess.run(list(argv), env=dict(env), timeout=timeout,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=False, shell=False, check=False)
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)
```

每日 ID 使用 `ZoneInfo(BACKUP_DAILY_TIMEZONE or "Asia/Shanghai")` 的日期；即时恢复点 ID 与 `purge_batch_id` 一一绑定。所有路径均由已校验 ID 拼接并再次验证位于 backup root 下。

- [ ] **Step 4: 写 manifest-last 和失败测试**

```python
def test_final_manifest_is_written_only_after_remote_dump_and_manifest_verify(service, storage, root):
    result = service.create_backup(BackupRequest.restore_point("batch-001"))
    assert storage.calls[-1] == ("download", result.manifest.remote_manifest_key)
    assert (root / result.manifest.backup_id / "manifest.json").exists()

def test_remote_hash_failure_leaves_attempt_failed_and_no_local_final_manifest(service, storage, root):
    storage.corrupt_downloads = True
    result = service.create_backup(BackupRequest.daily(date(2026, 8, 6)))
    assert result.status == "failed"
    assert result.stage == "remote_dump_verify"
    assert not (root / result.backup_id / "manifest.json").exists()

def test_retry_reconciles_identical_backup_without_overwrite(service, storage):
    first = service.create_backup(BackupRequest.restore_point("batch-001"))
    puts = list(storage.put_calls)
    second = service.create_backup(BackupRequest.restore_point("batch-001"))
    assert second.manifest.to_dict() == first.manifest.to_dict()
    assert storage.put_calls == puts

def test_retry_rejects_same_id_with_different_database_identity(service, runner):
    service.create_backup(BackupRequest.restore_point("batch-001"))
    runner.database_identity = "different-db"
    with pytest.raises(BackupConflictError):
        service.create_backup(BackupRequest.restore_point("batch-001"))

def test_complete_manifest_has_retention_and_unverified_production_gates(service):
    manifest = service.create_backup(BackupRequest.daily(date(2026, 8, 6))).manifest
    assert manifest.retention_days == 30
    assert manifest.retain_until > manifest.completed_at
    assert set(manifest.production_gates.values()) == {"not_verified"}

def test_local_dump_and_directories_are_private(service, root):
    manifest = service.create_backup(BackupRequest.daily(date(2026, 8, 6))).manifest
    directory = root / manifest.backup_id
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / manifest.artifact_file).stat().st_mode) == 0o600
```

- [ ] **Step 5: 实现本机 dump、异机校验与 final manifest**

```python
def create_backup(self, request: BackupRequest) -> BackupResult:
    self._validate_clients_and_server()
    artifact = self._create_or_reconcile_local_dump(request)
    self._store_and_verify_remote_dump(request, artifact)
    manifest = self._build_complete_manifest(request, artifact)
    self._store_and_verify_remote_manifest(manifest)
    self._write_local_manifest_atomic(manifest)
    return BackupResult.complete(manifest)
```

本机 staging 使用 0700 目录和 0600 文件；dump 后显式 `fsync`，`pg_restore --list` 成功后才计算 SHA-256 和原子发布。每次失败都原子写 `attempt-result.json`，列出 stage、error_code 和可能存在的 local/remote object identity，不含凭证。

- [ ] **Step 6: 实现 `verify_copies()`**

读取并验证 final manifest schema/status/binding；重新计算本机 SHA-256，并从 remote 下载到 0600 临时文件重算 SHA-256。unknown/partial/failed manifest 一律拒绝。

- [ ] **Step 7: 运行定向测试和静态检查**

Run: `cd backend && python -m pytest test/test_postgres_backup.py test/test_backup_storage.py -v`

Expected: all tests PASS, no PostgreSQL/OSS access.

Run: `git diff --check`

Expected: no output.

### Task 3: 安全的隔离恢复验证器

**Files:**
- Modify: `backend/services/postgres_backup.py`
- Test: `backend/test/test_postgres_restore_verification.py`

**Interfaces:**
- Consumes: Task 2 `BackupManifest`, `CommandRunner` 和 Task 1 `BackupStorage`。
- Produces: `RestoreVerificationConfig`, `RestoreVerificationResult`, `PostgresRestoreVerifier.verify_from_remote()`。

- [ ] **Step 1: 写安全门和命令契约测试**

```python
def test_restore_requires_disposable_config_and_acknowledgement(verifier, manifest):
    with pytest.raises(RestoreSafetyError, match="acknowledge"):
        verifier.verify_from_remote(manifest, acknowledge_isolated=False)

def test_restore_rejects_same_source_and_target_identity(verifier, manifest, runner):
    runner.restore_identity = manifest.database_identity
    with pytest.raises(RestoreSafetyError, match="源数据库"):
        verifier.verify_from_remote(manifest, acknowledge_isolated=True)

def test_restore_uses_generated_new_database_and_never_drops(verifier, manifest, runner):
    result = verifier.verify_from_remote(manifest, acknowledge_isolated=True)
    assert re.fullmatch(r"backup_verify_[0-9a-f]{32}", result.target_database)
    assert all(call.argv[0] != "dropdb" for call in runner.calls)

def test_restore_command_forbids_clean_and_create(verifier, manifest, runner):
    verifier.verify_from_remote(manifest, acknowledge_isolated=True)
    restore = next(call.argv for call in runner.calls if call.argv[0] == "pg_restore")
    assert "--exit-on-error" in restore
    assert "--single-transaction" in restore
    assert "--no-owner" in restore
    assert "--no-acl" in restore
    assert "--clean" not in restore
    assert "--create" not in restore
    assert all(call.argv[0] != "dropdb" for call in runner.calls)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest test/test_postgres_restore_verification.py -v`

Expected: failures because restore verifier is not implemented.

- [ ] **Step 3: 实现仅创建新库的恢复流程**

```python
def verify_from_remote(self, manifest, *, acknowledge_isolated):
    self._validate_gate(manifest, acknowledge_isolated)
    dump_path = self._download_and_verify(manifest)
    target = "backup_verify_" + self._uuid_factory().hex
    self._assert_database_absent(target)
    self._createdb(target)
    self._pg_restore(target, dump_path)
    evidence = self._query_structure_and_counts(target)
    self._validate_evidence(evidence)
    return RestoreVerificationResult.complete(target, evidence)
```

`psql` 检查返回单行 JSON，包含 `vector_extension`、`products_table`、`image_assets_table`、`vector_type`、`products_count`、`image_assets_count`。数据库密码仍只放 `PGPASSWORD` 环境。

- [ ] **Step 4: 覆盖目标存在、restore 失败和结构失败**

```python
def test_existing_generated_target_fails_without_restore(verifier, manifest, runner):
    runner.database_exists = True
    with pytest.raises(RestoreSafetyError, match="已存在"):
        verifier.verify_from_remote(manifest, acknowledge_isolated=True)
    assert all(call.argv[0] != "pg_restore" for call in runner.calls)

def test_restore_failure_keeps_database_and_returns_stable_stage(verifier, manifest, runner):
    runner.fail_program = "pg_restore"
    result = verifier.verify_from_remote(manifest, acknowledge_isolated=True)
    assert result.status == "failed"
    assert result.stage == "restore"
    assert all(call.argv[0] != "dropdb" for call in runner.calls)

def test_vector_dimension_mismatch_fails_with_structural_evidence(verifier, manifest, runner):
    runner.structure_evidence["vector_type"] = "vector(768)"
    result = verifier.verify_from_remote(manifest, acknowledge_isolated=True)
    assert result.status == "failed"
    assert result.stage == "structure_verify"
    assert result.evidence["vector_type"] == "vector(768)"

def test_result_never_contains_password_or_raw_dsn(verifier, manifest):
    payload = json.dumps(
        verifier.verify_from_remote(manifest, acknowledge_isolated=True).to_dict()
    )
    assert "restore-secret" not in payload
    assert "postgresql://" not in payload
```

- [ ] **Step 5: 运行定向测试和静态检查**

Run: `cd backend && python -m pytest test/test_postgres_restore_verification.py test/test_postgres_backup.py -v`

Expected: all tests PASS; fake runner records no `dropdb`.

Run: `git diff --check`

Expected: no output.

### Task 4: 显式 CLI 与 disposable 集成测试入口

**Files:**
- Create: `backend/scripts/manage_postgres_backups.py`
- Create: `backend/test/test_manage_postgres_backups.py`
- Create: `backend/test/integration/test_postgres_backup_restore.py`

**Interfaces:**
- Consumes: Tasks 1–3 services。
- Produces: `main(argv=None, environ=None, runner_factory=SubprocessCommandRunner, storage_factory=OssBackupStorage.from_env, stdout=sys.stdout, stderr=sys.stderr) -> int`。

- [ ] **Step 1: 写 CLI 模式、退出码和脱敏测试**

```python
@pytest.mark.parametrize("argv", [
    ["create-daily"],
    ["create-restore-point", "--purge-batch-id", "batch-001"],
    ["verify-copies", "--manifest", "manifest.json"],
    ["verify-restore", "--manifest", "manifest.json", "--acknowledge-isolated"],
])
def test_cli_emits_one_json_result(argv, cli_dependencies):
    stdout = io.StringIO()
    code = main(argv, stdout=stdout, stderr=io.StringIO(), **cli_dependencies)
    assert code == 0
    assert json.loads(stdout.getvalue())["status"] in {"complete", "verified"}

@pytest.mark.parametrize("failure, expected", [
    ("config", 2), ("dump", 3), ("storage", 4), ("restore", 5),
])
def test_cli_maps_config_dump_storage_and_restore_failures_to_2_3_4_5(
    failure, expected, failing_cli_dependencies
):
    code = main(["create-daily"], **failing_cli_dependencies(failure))
    assert code == expected

def test_cli_output_does_not_contain_secret_or_dsn(cli_dependencies):
    stderr = io.StringIO()
    main(["create-daily"], stdout=io.StringIO(), stderr=stderr, **cli_dependencies)
    assert "secret-value" not in stderr.getvalue()
    assert "postgresql://" not in stderr.getvalue()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest test/test_manage_postgres_backups.py -v`

Expected: import failure because the CLI does not exist.

- [ ] **Step 3: 实现四子命令 CLI**

CLI 默认读取 `backend/.env.backup`，只把显式环境映射交给配置对象；不调用 Flask app、不导入应用 DB session。每次只输出一个 JSON object。

```python
EXIT_CONFIG = 2
EXIT_INTEGRITY = 3
EXIT_STORAGE = 4
EXIT_RESTORE = 5

def main(argv=None, *, environ=None, runner_factory=SubprocessCommandRunner,
         storage_factory=OssBackupStorage.from_env, stdout=sys.stdout, stderr=sys.stderr):
    args = create_parser().parse_args(argv)
    environment = dict(os.environ if environ is None else environ)
    try:
        return dispatch(args, environment, runner_factory, storage_factory, stdout)
    except BackupConfigError as error:
        write_failure(stderr, "config", error)
        return EXIT_CONFIG
    except BackupIntegrityError as error:
        write_failure(stderr, "integrity", error)
        return EXIT_INTEGRITY
    except BackupStorageError as error:
        write_failure(stderr, "storage", error)
        return EXIT_STORAGE
    except RestoreVerificationError as error:
        write_failure(stderr, "restore", error)
        return EXIT_RESTORE
```

- [ ] **Step 4: 添加显式 gated disposable 集成测试**

集成测试在以下任一条件不满足时 `pytest.skip`：`RUN_DISPOSABLE_BACKUP_RESTORE_TEST=1`、`RESTORE_VERIFY_DISPOSABLE=1`、完整 `DISPOSABLE_SOURCE_ADMIN_DB_*`/`BACKUP_DB_*`/`RESTORE_VERIFY_DB_*` 和 fake remote storage fixture。它必须自己创建随机 disposable 源数据库与固定样本，不依赖预置业务数据；不得使用应用 `DB_*`、真实 OSS 或自动删除源/恢复数据库。

```python
def test_custom_dump_restores_pgvector_schema_and_rows(disposable_environment, fake_remote_storage):
    backup_service = build_real_backup_service(disposable_environment, fake_remote_storage)
    restore_verifier = build_real_restore_verifier(disposable_environment, fake_remote_storage)
    manifest = backup_service.create_backup(
        BackupRequest.daily(date(2026, 8, 6))
    ).manifest
    result = restore_verifier.verify_from_remote(manifest, acknowledge_isolated=True)
    assert result.evidence["vector_type"] == "vector(1024)"
    assert result.evidence["image_assets_count"] == 1
```

- [ ] **Step 5: 运行 fake CLI 测试并确认集成测试安全 skip**

Run: `cd backend && python -m pytest test/test_manage_postgres_backups.py test/integration/test_postgres_backup_restore.py -v`

Expected: fake CLI tests PASS; disposable integration test SKIPPED with an explicit missing-gate reason; no database/storage access.

### Task 5: 专用配置样例、恢复手册和演练模板

**Files:**
- Create: `backend/.env.backup.example`
- Create: `docs/operations/postgresql-backup-restore-runbook.md`
- Create: `docs/operations/templates/postgresql-restore-drill-record.md`
- Modify: `.gitignore`

**Interfaces:**
- Documents: CLI from Task 4 and production gates from the design spec。

- [ ] **Step 1: 写专用 env 样例**

只包含非真实的示例值：`BACKUP_DB_*`、`BACKUP_ROOT`、`BACKUP_DAILY_TIMEZONE=Asia/Shanghai`、`BACKUP_OSS_*`、`RESTORE_VERIFY_DB_*`、`RESTORE_VERIFY_DISPOSABLE=0`。明确该文件只进入独立 ops 进程，绝不能注入 Flask/Gunicorn。

- [ ] **Step 2: 写可执行恢复手册**

手册固定 PostgreSQL 16 client prerequisite，给出 `create-daily`、`create-restore-point`、`verify-copies`、`verify-restore` 命令；每步标出预期 JSON 状态、失败停止条件、partial/orphan 处置和 production gate。不得包含真实 DSN、凭证或签名 URL。

- [ ] **Step 3: 写演练记录模板**

模板分开记录：自动化测试证据、备份桶私有/SSE/生命周期/IAM 证据、从异机副本恢复的真实隔离演练证据、待授权项。明确 fake 测试不能勾选真实 production gate。

- [ ] **Step 4: 忽略本地备份与专用 env**

在 `.gitignore` 增加：

```gitignore
.env.backup
backups/
```

- [ ] **Step 5: 运行文档/敏感信息扫描**

Run: `rg -n "(postgresql|postgres)://|AKIA|LTAI|BEGIN (RSA|OPENSSH) PRIVATE KEY" backend/.env.backup.example docs/operations`

Expected: no output.

Run: `rg -n "not_verified|禁止|不得|30 天|PostgreSQL 16" docs/operations backend/.env.backup.example`

Expected: matches every required production gate and safety boundary.

### Task 6: 综合定向验证与交付前状态检查

**Files:**
- Verify only; no new file required.

**Interfaces:**
- Consumes all prior tasks.

- [ ] **Step 1: 运行全部 T10 fake 单元测试**

Run: `cd backend && python -m pytest test/test_backup_storage.py test/test_postgres_backup.py test/test_postgres_restore_verification.py test/test_manage_postgres_backups.py -v`

Expected: all tests PASS; no PostgreSQL/OSS connection.

- [ ] **Step 2: 验证 disposable 集成测试默认安全跳过**

Run: `cd backend && env -u RUN_DISPOSABLE_BACKUP_RESTORE_TEST python -m pytest test/integration/test_postgres_backup_restore.py -v`

Expected: one SKIPPED result naming the missing explicit gate.

- [ ] **Step 3: 编译和 diff 检查**

Run: `cd backend && python -m compileall services/backup_storage.py services/postgres_backup.py scripts/manage_postgres_backups.py`

Expected: compilation succeeds.

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only T10 files are modified/untracked.

- [ ] **Step 4: 独立 risk reviewer 审查**

Reviewer 必须只读核对：凭证泄露、路径穿越、命令注入、错误阶段、manifest-last、幂等绑定、远端哈希、恢复误伤、production gate 夸大、真实外部写入风险和 Issue #24 验收覆盖。所有 MUST FIX 修正后复跑 Task 6。

- [ ] **Step 5: 最终报告**

报告基线从 `main@5e91cc2` 调整到 detached `088bb9f`、变更文件、测试原始输出、architect/risk reviewer 结论、未运行的真实 disposable 恢复、仍需人工授权的 production gates、潜在合并冲突；不得声称 Issue #24 的真实恢复演练已完成。
