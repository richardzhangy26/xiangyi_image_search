# 永久清除批次 Worker 运行手册

## 范围与默认状态

本手册只描述永久清除批次在 `pending_deletion` 前的备份、复验和恢复证据。它不授权正式对象、资产记录、向量、备份副本或孤儿对象的删除；任何残留恢复点、副本或 orphan 均由管理员人工登记和处置。

`purge-batch-worker` 是唯一加载 `backend/.env.backup` 的应用容器，也是唯一写入 worker 能力证明的容器。Flask、常规图片导入 worker、cleanup 和 frontend 不得加载该文件或其 ops 凭证。

## Compose 证据卷与挂载

两类证据不能混用，目录也不能互相挂载替代：

| 证据 | 环境变量与容器路径 | 访问规则 |
| --- | --- | --- |
| 五项安全门证据 | `PURGE_GATE_EVIDENCE_DIR=/app/purge-gate-evidence` | `purge_gate_evidence:/app/purge-gate-evidence:ro` 同时只读挂载给 backend 与 purge-batch-worker；外部 ops 证据发布者是唯一写入者。 |
| worker 能力证明 | `PURGE_PIPELINE_EVIDENCE_DIR=/app/purge-evidence` | backend 使用 `purge_pipeline_evidence:/app/purge-evidence:ro`；purge-batch-worker 使用 `purge_pipeline_evidence:/app/purge-evidence` 写入。 |

部署前必须由受信任的卷管理员初始化 `purge_pipeline_evidence`：目录归属为 purge-batch-worker 的 `worker UID`，权限只允许该 UID 写入。backend 只能以只读方式挂载，不能通过组权限、辅助容器或宿主机临时挂载获得写权限。`purge_gate_evidence` 的写权限只交给独立 ops 证据发布者；两个应用容器都是只读消费者。

能力文件固定为 `purge_batch_worker.json`，只允许 worker 写入。它的 TTL 是 120 秒，worker 每 30 秒刷新 heartbeat；任一刷新失败、文件损坏或到达过期时刻都使 pipeline 不可用。五项安全门各自的过期窗口独立判定，不能用有效的能力 heartbeat 延长或替代安全门证据。

## 人工处置边界

`PURGE_BACKUP_RETENTION_EXPIRED` 表示恢复点或对象副本保留证据缺失/到期。该批次不得重试；只能取消后以新批次 ID、新 `Idempotency-Key` 和新确认重新开始。后续正式删除前仍须在写入 fence、锁或等效串行化边界重新核对完整引用与保留期；本仓库不自动清理任何恢复点、对象副本或 orphan。

## Worker 运行时

- 镜像目标 `purge-batch-worker-runtime` 与 backend 镜像分开标记为 `fashion-crm-purge-batch-worker:latest`。
- 入口以 root 仅执行 `chown -R 1000:1000 /app/purge-evidence /var/lib/purge-batch-worker`，然后 `setpriv` 降到 UID 1000 再执行 worker；root 阶段不加载 ops 环境、不写能力证明、不做备份。
- `purge_batch_worker_state:/var/lib/purge-batch-worker` 是 worker 独占持久根；Compose 覆盖 `BACKUP_ROOT=/var/lib/purge-batch-worker/postgres` 与 `PURGE_OBJECT_BACKUP_LOCAL_ROOT=/var/lib/purge-batch-worker/object-manifests`。#26 不删除该卷内容。
- 引用快照最大时效 `PURGE_REFERENCE_SNAPSHOT_MAX_AGE_SECONDS=60`，由 `PostgresReferenceSnapshotReader` 在 worker 内提供。
- 启动前比较队列库与 `BACKUP_DB` 的 `current_database()` / `system_identifier`；不一致只写失败能力证明，不领取批次。

能力证明只允许键：`schema_version`、`component`、`result`、`verified_at`、`expires_at`、`policy`、`summary`。递归拒绝含 `password`、`secret`、`token`、`authorization`、`dsn` 的键。

稳定错误码包括：`INVALID_PURGE_IDEMPOTENCY_KEY`、`PURGE_IDEMPOTENCY_CONFLICT`、`PURGE_ASSET_IN_ACTIVE_BATCH`、`PURGE_BATCH_NOT_CANCELLABLE`、`PURGE_BATCH_NOT_RETRYABLE`、`PURGE_PIPELINE_UNAVAILABLE`、`PURGE_ASSET_RESTORE_BLOCKED`、`PURGE_GATE_NOT_READY`、`PURGE_BACKUP_RETENTION_EXPIRED`、`PURGE_DATABASE_BACKUP_FAILED`、`PURGE_OBJECT_BACKUP_FAILED`、`PURGE_OBJECT_VERIFICATION_FAILED`、`PURGE_REFERENCE_SNAPSHOT_INVALID`。

## 人工残留台账

残留恢复点、对象副本、partial/orphan 只能由管理员人工登记和处置。禁止对本仓库暴露的对象备份、隔离恢复或备份 Bucket 调用 Delete。#27 若实施正式删除，必须在锁或写入 fence 内重新验证全部引用、正式对象身份和保留期。
