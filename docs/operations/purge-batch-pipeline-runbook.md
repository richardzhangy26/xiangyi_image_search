# 永久清除批次 Worker 运行手册

## 范围与默认状态

本手册描述永久清除批次的备份、复验、`pending_deletion` 可观测性与当前**硬性关闭**边界。生产 backup worker 在 `pending_deletion` 中止：它不加载删除凭证、不组合 `FormalPurgeRepository` 或 deleter，因而不能删除正式对象、资产记录或向量。T14 新增的独立 `formal-purge-worker` 只是默认不启动的 one-shot 组合根；当前 main 只使用恒 false capability，并在构造 repository/对象客户端前退出。typed grant、正式对象身份、检查点和 re-protection 代码均不构成真实删除授权。

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

## T14 正式删除启用前提

T14 是唯一可改变正式删除硬关闭状态的人工授权点。启用前必须验证每项 canonical manifest 成员身份、备份副本与保留期、正式对象身份、完整引用、对象 binding/deletion fence 和 OSS 无外部覆盖信任边界；还必须提供专用最小权限删除凭证。未完成这些现场验证时，禁止向 `purge-batch-worker` 注入删除凭证、deleter 或任何启用证据。

正式删除采用“全部正式 writer 强制 binding fence + IAM 禁止外部覆盖”的 no-overwrite trust 模式；exact-version 不在当前代码授权范围。`PURGE_FORMAL_DELETION_DEPLOYED=1` 时如果 `INGEST_BINDING_FENCE_ENABLED` 未开启，HTTP writer 构造必须失败。grant 中的 writer inventory digest 必须与代码固定清单一致。正式删除 grant 最长 15 分钟且绑定环境、部署、单一批次/资产和两份 manifest；worker 在每次 intent、授权、对象 Delete 与数据库最终化前重新读取，撤销后停止未来调用。

保留期跨越后，未到 delete intent 的项可释放 fence 并标为 reprotected；已有 intent 或对象状态未知的项进入 `PURGE_REPROTECTION_REQUIRED`，保留 fence 与资产预约。只有 original 及需删除 preview 的 Bucket/Key/SHA-256 重新证明与备份快照一致，才能释放 fence并允许回收站恢复。该确认 seam 不会自行写回对象。

## Worker 运行时

- 镜像目标 `purge-batch-worker-runtime` 与 backend 镜像分开标记为 `fashion-crm-purge-batch-worker:latest`。
- 入口以 root 仅执行 `chown -R 1000:1000 /app/purge-evidence /var/lib/purge-batch-worker`，然后 `setpriv` 降到 UID 1000 再执行 worker；root 阶段不加载 ops 环境、不写能力证明、不做备份。
- `purge_batch_worker_state:/var/lib/purge-batch-worker` 是 worker 独占持久根；Compose 覆盖 `BACKUP_ROOT=/var/lib/purge-batch-worker/postgres` 与 `PURGE_OBJECT_BACKUP_LOCAL_ROOT=/var/lib/purge-batch-worker/object-manifests`。#26 不删除该卷内容。
- 引用快照最大时效 `PURGE_REFERENCE_SNAPSHOT_MAX_AGE_SECONDS=60`，由 `PostgresReferenceSnapshotReader` 在 worker 内提供。
- 启动前比较队列库与 `BACKUP_DB` 的 `current_database()` / `system_identifier`；不一致只写失败能力证明，不领取批次。
- `python -m scripts.manage_purge_schema plan` 完全离线；`check` 只读盘点关键表/列/index；`apply` 必须同时提供 `--acknowledge-additive` 和已审 plan SHA-256。任何迁移仍需另行授权，本手册不授权执行。
- `python -m scripts.check_formal_purge_health --evidence <path>` 只读检查独立 formal health evidence，返回稳定 0/2；厂商监控与通知路由属于 `AUTH-28-06`。

能力证明只允许键：`schema_version`、`component`、`result`、`verified_at`、`expires_at`、`policy`、`summary`。递归拒绝含 `password`、`secret`、`token`、`authorization`、`dsn` 的键。

稳定错误码包括：`INVALID_PURGE_IDEMPOTENCY_KEY`、`PURGE_IDEMPOTENCY_CONFLICT`、`PURGE_ASSET_IN_ACTIVE_BATCH`、`PURGE_BATCH_NOT_CANCELLABLE`、`PURGE_BATCH_NOT_RETRYABLE`、`PURGE_PIPELINE_UNAVAILABLE`、`PURGE_ASSET_RESTORE_BLOCKED`、`PURGE_GATE_NOT_READY`、`PURGE_BACKUP_RETENTION_EXPIRED`、`PURGE_DATABASE_BACKUP_FAILED`、`PURGE_OBJECT_BACKUP_FAILED`、`PURGE_OBJECT_VERIFICATION_FAILED`、`PURGE_REFERENCE_SNAPSHOT_INVALID`。

## 人工残留台账

残留恢复点、对象副本、partial/orphan 只能由管理员人工登记和处置。禁止对本仓库暴露的对象备份、隔离恢复或备份 Bucket 调用 Delete。#27 若实施正式删除，必须在锁或写入 fence 内重新验证全部引用、正式对象身份和保留期。
