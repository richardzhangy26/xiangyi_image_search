# 永久清除对象备份清单与隔离恢复设计

## 目标与边界

本设计实现 Issue #25：在永久清除流水线进入正式删除前，根据同一时刻的完整引用关系，识别本批图片资产的独占源图和将失去最后引用的共享搜索预览图，把这些正式对象复制到独立私有备份 Bucket，逐项校验并生成与 PostgreSQL 恢复点使用同一 `purge_batch_id` 的不可变清单。

本 Ticket 不创建永久清除批次、不实现认证或 worker、不删除正式对象、资产记录或向量，也不开放正式删除入口。真实 PostgreSQL、正式 OSS、备份 Bucket、隔离恢复 Bucket、凭证、部署和恢复演练均不连接或配置；自动化只使用完整引用快照 fake 与内存对象存储 fake。

## 架构审查结论

采用两个深 Module：

1. `PurgeObjectBackupService` 隐藏引用判定、源对象实字节校验、不可覆盖复制、重试 reconcile 和 manifest-last 提交协议。
2. `PurgeObjectRestoreService` 隐藏 final manifest 校验、备份对象复验、隔离目标派生、不可覆盖恢复和恢复后读回校验。

两者使用不同的 composition root。备份进程只需要正式 OSS 只读凭证与备份 Bucket 的 Put/Head/Get 凭证；恢复进程只需要备份 Bucket 的 Head/Get 凭证与隔离 Bucket 的 Put/Head/Get 凭证。Flask 日常应用不装配这两个 Module。

不把创建对象备份做成独立 CLI。后续 Issue #26 必须在认证、持久批次和数据库恢复点状态机中调用创建 Interface，避免出现绕过安全门的第二入口。本 Ticket 只提供校验既有副本与恢复到隔离位置的显式运维 CLI。

## 外部 Interface

### 对象备份

```python
PurgeObjectBackupService.create_verified(
    PurgeObjectBackupRequest(
        purge_batch_id="batch-001",
        asset_ids=("asset-a", "asset-b"),
    )
) -> VerifiedPurgeObjectBackup

PurgeObjectBackupService.revalidate_current_candidates(
    manifest
) -> CurrentDeletionCandidates
```

`create_verified` 的调用方只提供批次与资产身份。数据库恢复点由 `RestorePointGate.require_verified(purge_batch_id)` 返回；资产字段和全部引用由 `ReferenceSnapshotReader.capture_for_purge(asset_ids)` 返回。调用方不能提供正式对象 Key、对象大小、哈希、引用计数或备份 Key。

`revalidate_current_candidates` 重新捕获完整引用快照，只能返回已经备份且当前仍可成为删除候选的对象集合。引用减少导致某个先前未备份对象成为最后引用时必须失败，不能把它补进删除集合；引用增加时可以安全缩减候选集合。

### 隔离恢复

```python
PurgeObjectRestoreService.verify_copies(manifest) -> ObjectCopyVerification

PurgeObjectRestoreService.restore_to_isolation(
    manifest,
    restore_run_id="drill-001",
    acknowledge_isolated=True,
) -> IsolatedObjectRestoreResult
```

恢复目标 Bucket 与前缀只来自独立配置。目标 Key 由 Module 根据 `restore_run_id`、批次和对象身份摘要生成，调用方不能提交正式 Key 或任意目标 Key。

## 完整引用快照合同

引用目录 v1 固定包含：

- `image_assets`：`active` 与 `archived` 都是有效引用。
- `image_import_items`：所有未结束状态都是有效引用；即使当前没有该表或没有行，也必须返回显式完整空切片。

快照至少包含 `catalog_version`、`consistency_token`、`captured_at`、目标资产表示、每个来源切片和逐对象引用边。所有切片必须来自同一 consistency token，声明数量必须等于实际边数，且 `truncated=false`、`status=complete`。

消费时快照默认不得早于当前时钟 300 秒，也不得超前超过 60 秒；未来生产 Adapter 每次捕获都必须生成新鲜 `captured_at`。Adapter 应把图片导入的各个数据库非终态映射为目录 v1 的稳定语义状态 `unfinished`，不能把未知状态静默当作有效引用。

以下任一情况 fail closed：

- 缺少、重复或出现未知引用来源；
- 任一切片 partial、error、truncated 或 consistency token 不一致；
- 请求资产与快照目标不精确相等；
- 目标缺失、不是已归档图片，或缺少源图/搜索预览图引用；
- 同一正式 `(bucket, key)` 同时作为源图和搜索预览图；
- 源图不是恰好一个且属于当前目标资产的独占引用；
- 对象 Key、已知大小、哈希或状态不符合合同。

当前 Ticket 只提供 fake Adapter。未来 PostgreSQL Adapter 必须在一个只读、可重复读事务中读取目标资产，再对目标 Key 枚举上述全部来源的入边；该 Adapter 与真实事务语义在本轮均未验证。

## 引用决策

Module 按正式 `(bucket, key)` 建立入边索引：

- 每张目标资产的源图只有在唯一入边就是该资产时进入备份清单；共享或异常源图整批失败。
- 搜索预览图只有在移除本批目标资产引用后没有任何其他有效引用时进入备份清单。
- 多个本批资产共享同一最后引用搜索预览图时，只生成一个对象项，并记录全部关联资产 ID。
- 仍由批外图片资产或未结束图片导入项引用的搜索预览图记录为 `reference_protected`，不复制，也不交给后续删除。

现有搜索预览 metadata 中的 `sha256` 是源图内容哈希，不是预览文件本体哈希。搜索预览图必须下载实际字节重新计算 SHA-256；不能把 `ImageAsset.content_hash` 或 OSS ETag 当作预览 SHA-256。

## PostgreSQL 恢复点绑定

`RestorePointGate` 必须重用 Issue #24 的严格 `BackupManifest` 合同与副本验证，并要求：

- `kind == purge_restore_point`；
- `purge_batch_id` 与请求完全一致；
- `backup_id == f"purge-{purge_batch_id}"`；
- final manifest 与本机、异机副本已通过 #24 校验。

对象备份使用相同 `purge_batch_id`，并继承数据库恢复点的 `retain_until`。对象 manifest 记录数据库恢复点 backup ID、远端 manifest Key 与 canonical SHA-256，但不修改 #24 exact-schema final manifest。

## 复制与提交协议

1. 验证请求、数据库恢复点和完整引用快照。
2. 选择独占源图与最后引用搜索预览图，规范排序并计算引用快照摘要。
3. 对每个正式对象执行 HEAD，下载到 0600 临时文件并计算实际大小与 SHA-256；源图同时核对数据库 `source_size/content_hash`。
4. 用 `sha256(formal_bucket + "\\0" + formal_key)` 生成不包含用户路径的稳定对象 ID；从配置前缀、数据库 backup ID 和对象 ID 派生备份 Key。
5. 无覆盖写入并下载读回不可变 `plan.json`。它在任何 payload 写入前列出正式对象、目标备份 Key、实际大小与哈希，使后续 partial payload 可追踪。
6. 对每项执行目标 HEAD。不存在时用 forbid-overwrite Put；已存在时只允许批次、对象身份、大小、哈希 metadata 全部一致。
7. Put 或 reconcile 后重新 HEAD，并独立下载目标对象重新计算大小与 SHA-256。
8. 重新捕获完整引用快照；语义摘要变化时失败，不写 complete manifest。
9. 所有对象通过后才无覆盖写入 `manifest.json`，再下载逐字节核对；它是对象备份的 final commit marker。

同一批次重试必须使用相同 plan 与确定性 Key。对象、选择、引用摘要或数据库恢复点绑定变化时稳定冲突，绝不覆盖。失败时已写入的 plan 与 payload 保留给同批次 reconcile；本 Ticket 不清理它们。

## Final manifest 合同

final manifest 使用 exact-schema canonical JSON，至少记录：

- schema、`status=complete`、`kind=purge_object_backup`；
- `purge_batch_id`、数据库 `backup_id`、远端 Bucket 和数据库 manifest identity/digest；
- 排序后的资产 ID、引用目录/快照摘要；
- 每项对象类型、关联资产、正式 Bucket/Key、备份 Bucket/Key；
- 实际字节数与实际 SHA-256；
- 源 HEAD/下载、备份 HEAD/下载的验证状态；
- 因引用保护而保留的搜索预览图决策；
- `retention_days=30` 与数据库恢复点相同的 `retain_until`；
- `authorization=backup_only_no_delete`，清单不能表达删除授权；
- 所有真实环境 production gate 均为 `not_verified`。

清单不得包含凭证、签名 URL、原始 DSN、图片内容或 embedding 向量。

## 隔离恢复协议

1. 严格解析 complete manifest，并下载备份 Bucket 中的 final manifest 逐字节核对。
2. 要求 `acknowledge_isolated=true`，配置声明目标为隔离环境，且隔离 Bucket 与正式、备份 Bucket 均不同。
3. 逐项下载备份 payload，按 manifest 重新核对大小与 SHA-256。
4. 目标 Key 固定为 `<isolated-prefix>/<restore-run-id>/<database-backup-id>/objects/<object-id>`，不复用正式 Key。
5. 目标不存在才不可覆盖 Put；存在时只允许完整一致的幂等复用。
6. Put 后重新 HEAD，再独立下载并重新核对大小与 SHA-256。
7. 返回脱敏的稳定恢复结果；不签名公开 URL，不删除任何对象。

## 凭证与生产门

- 正式源读取使用独立 Head/Get-only 凭证，不回退应用 `OSS_*`。
- 备份写入复用 #24 的独立 `BACKUP_OSS_*`，Interface 只有 Put/Head/Get，无 Delete。
- 隔离恢复使用独立 `PURGE_RESTORE_OSS_*`，不得与正式或备份 Bucket/凭证相同。
- Flask、Gunicorn、frontend、健康检查和普通部署不加载上述 ops 凭证。
- 代码中的无 Delete Interface、私有/SSE 请求头和 fake 测试不能证明真实 IAM、Bucket ACL、SSE、30 天生命周期/Object Lock 或真实恢复能力；这些门在 T14 人工验证前保持关闭。

## TOCTOU 与后续 Ticket

对象备份 manifest 只是备份证据，不是正式删除授权。Issue #26 在进入待删除状态前必须重新调用当前引用重验；Issue #27 在实际删除共享搜索预览图前还必须在事务锁或写入 fence 下再次检查所有图片资产和未结束导入项引用。任何摘要变化都必须 fail closed。

## 测试范围

- 完整/不完整引用快照与 catalog fail-closed。
- 独占源图、批内共享最后引用预览、批外资产引用和未结束导入项引用。
- PostgreSQL 恢复点批次绑定与 30 天期限继承。
- source HEAD/download 实字节哈希、不可变 plan、无覆盖复制、reconcile、冲突和 final manifest-last。
- Put 后独立 HEAD/download 大小与 SHA-256 复验。
- final manifest 严格解析、隔离 Bucket/确认门、程序派生目标 Key、幂等恢复和冲突。
- 配置/静态合同确认不回退应用凭证、无 Delete Interface、无正式删除调用。

真实 PostgreSQL/OSS、真实 IAM、真实 Bucket、真实凭证、真实恢复和部署均不属于本轮自动化证据。
