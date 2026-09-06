# Issue #28 T14 生产恢复演练与受控启用实施计划

> **For agentic workers:** 后续实施必须逐阶段执行；每个标为“需授权”的阶段都是独立停止点，不能把本 Issue、`ready-for-human` 标签、前一阶段授权或测试通过解释为后一阶段授权。涉及代码实现时使用项目规定的 Matt/TDD 流程；涉及真实环境时必须由授权人员操作或逐项明确授权后再执行。

**Goal:** 在不把备份证据误当删除授权的前提下，补齐 T14 的生产执行安全合同，完成隔离恢复演练，并只在全部证据新鲜、代码阻塞项清零和再次取得精确试删授权后受控启用正式永久清除。

**Architecture:** 继续保留 #26 的备份 worker 和 `backup_only_no_delete` 能力证明，另设默认关闭、批次绑定、短时、可撤销的正式删除授权与独立运行组合。正式删除 worker 必须在每次不可逆外部调用前重新验证授权、manifest、副本、保留期、当前引用、对象身份及完整围栏集；快速关闭只停止未来调用，不宣称撤销在途或已完成的删除。

**Tech Stack:** Flask、SQLAlchemy、PostgreSQL 16 + pgvector、Aliyun OSS、Docker Compose、pytest、现有 PostgreSQL/对象备份与恢复 CLI、外部监控与安全证据发布器。

## Global Constraints

- 本计划本身不授权真实云读取/写入/删除、部署、数据库迁移、数据库恢复、服务启动、Git commit/push/PR 或 GitHub Issue 修改。
- 本轮只允许仓库静态盘点、本地 fake/测试库验证和编写本计划；截至本文生成时没有执行任何真实 OSS、生产数据库、部署或恢复命令。
- 真实环境的每个动作都必须引用一条精确授权记录；授权必须写明环境、账号/区域、数据库、Bucket/前缀、动作类别、批次/对象上限、时间窗口、操作者、复核人、证据落点和停止条件。
- Kodo 始终只读且不参与 T14；不执行迁移，不触碰退休 `product_images`。
- `backup_only_no_delete` manifest、安全门五项证据和 worker heartbeat 都不是正式删除授权。
- 正式删除能力默认且持续为关闭；缺文件、坏 JSON、未知字段、证据过期、时钟异常、权限身份不一致、监控不可用、任一复验失败或无法确定时都必须失败关闭。
- 不自动清理本机备份、远端副本、partial/orphan、隔离数据库、隔离对象、围栏或审计记录。任何清理另行授权。
- 快速关闭只能阻止尚未发起的 Delete。已发起或已完成的对象删除、资产行删除和向量删除不能由关闭开关撤销；恢复必须依赖已验证副本并另行授权。
- 当前对象恢复工具只恢复到隔离 Bucket，不支持自动写回正式 Key；在另行设计、演练并授权生产写回以前，不能宣称真实试删具备自动回滚。

---

## 1. 当前结论：NO-GO

截至 2026-08-31，Issue #28 的九条验收标准均未达到“可启用正式删除”的状态。当前只具备离线算法/失败关闭合同和一部分 PostgreSQL 测试证据；没有任何可接受的生产运行证据，也没有完整的正式删除生产组合根。

### 1.1 已有代码与文档能力

| 领域 | 现有事实 | 能证明什么 | 不能证明什么 |
| --- | --- | --- | --- |
| PostgreSQL 备份 | `backend/scripts/manage_postgres_backups.py`、`backend/services/postgres_backup.py` | 30 天 manifest 合同、本机与异机 SHA-256、不可覆盖 reconcile、隔离恢复安全门 | 每日调度确实持续运行、真实副本存在、真实恢复成功 |
| 对象备份/恢复 | `backend/services/purge_object_backup.py`、`backend/scripts/manage_purge_object_backups.py` | plan 先于 payload、manifest 最后提交、哈希读回、只恢复隔离位置 | 真实 Bucket/IAM、生命周期、真实隔离恢复成功 |
| 安全门 | `backend/services/purge_safety_gate.py`、`docs/operations/purge-gate-evidence.md` | 五项合取、未知/失败/过期关闭、敏感键拒绝 | 证据发布者可信、五项真实发生 |
| worker 能力证明 | `backend/services/purge_pipeline_capability.py` | 120 秒 TTL、30 秒 heartbeat、`backup_only_no_delete` | 正式删除能力、生产监控/告警、真实重启恢复 |
| 生产组合 | `backend/scripts/run_purge_batch_worker.py` | #26 备份/对象复验止于 `pending_deletion`，不读删除凭证 | 不能执行正式对象、资产行或向量删除 |
| 正式清除 seam | `backend/services/formal_purge.py` | fake deleter 下的 claim、检查点、部分失败、租约与向量行移除测试 | 真实 manifest 兼容、精确对象删除、生产授权和快速关闭 |
| 运维文档 | `docs/operations/` 四份 runbook 与两份演练模板 | 现有边界和证据字段 | 不是现场证据，不得勾选 production gate |

### 1.2 已存在的证据文件盘点

- 活动工作区中没有 `PURGE_GATE_EVIDENCE_DIR` 的五个运行证据 JSON，没有 `purge_batch_worker.json`，没有 PostgreSQL backup final manifest，没有对象 backup final manifest，也没有已填写的恢复演练记录。
- `backend/data` 下未发现可用于 T14 的证据文件。
- `backend/reports/issue-10`、`backend/reports/issue-11` 及旧 worktree 中的报告属于 Kodo 迁移历史，不是 T14 备份、恢复、安全门或删除授权证据，严禁复用。
- Docker named volume、生产主机、对象存储和调度器状态均未检查；读取这些真实环境状态也要先取得明确授权。

### 1.3 本轮本地验证记录

以下命令只使用 fake、本地临时文件或项目独立测试库，没有启动服务或访问 OSS：

```bash
cd backend
python -m pytest \
  test/test_issue_23_auth_unit.py \
  test/test_issue_23_gate_unit.py \
  test/test_issue_26_capability_unit.py \
  test/test_issue_26_worker_unit.py \
  test/test_issue_26_worker_static_contract.py \
  test/test_backup_storage.py \
  test/test_postgres_backup.py \
  test/test_postgres_restore_verification.py \
  test/test_manage_postgres_backups.py \
  test/test_purge_object_backup.py \
  test/test_purge_object_restore.py \
  test/test_purge_object_storage.py \
  test/test_manage_purge_object_backups.py \
  test/test_purge_object_backup_contract.py \
  test/test_issue_27_deletion_isolation.py \
  test/test_issue_27_delete_authorization_unit.py \
  test/test_issue_27_authorization_verifier.py \
  test/test_issue_27_formal_purge_unit.py -v
```

结果：`148 passed`。

```bash
cd backend
python -m pytest \
  test/integration/test_issue_23_purge_gate.py \
  test/integration/test_issue_26_restore_locking.py \
  test/integration/test_issue_27_formal_purge_repository.py \
  test/integration/test_issue_27_formal_purge_multisession.py \
  test/integration/test_postgres_backup_restore.py -v
```

结果：`19 passed, 1 skipped`。跳过项是 `test_custom_dump_restores_pgvector_schema_and_rows`，原因是未取得 `RUN_DISPOSABLE_BACKUP_RESTORE_TEST=1` 的显式门；因此真实 custom dump/restore 仍未验证。

---

## 2. 启用前代码阻塞项

以下任一项未完成时，不得请求注入删除凭证、启动正式删除 worker 或执行真实试删。

### B1：`verifying → pending_deletion` 没有落逐项授权快照

`run_purge_batch_worker.py` 当前从 `handle_verifying()` 返回通用 `'pending_deletion'`，随后调用 `advance_if_current()`；它没有从 canonical `PurgeObjectBackupManifest` 构造每个 item 的授权，也没有调用已存在的 `advance_verified_to_pending_if_current()`。结果是生产批次没有正式删除所需的 `formal_bucket`、original/preview key、backup object ID、digest 与 `authorization_retain_until` 完整快照。

处置：实现一个只接受真实 `PurgeObjectBackupManifest` 的授权映射器；验证每个目标资产恰有一个 source image 备份项，preview 按 `reference_protected`/已备份候选准确判定；把 `formal_bucket` 列为每项必填，允许删除 preview 时还必须要求 preview backup object ID/digest；以同一事务调用 `advance_verified_to_pending_if_current()`。manifest 缺项、重复项、Bucket/批次/哈希/保留期不一致时保持 `verifying` 或失败，绝不进入 `pending_deletion`。

### B2：canonical verifier 与真实 manifest schema 不兼容

`CanonicalFormalPurgeAuthorizationVerifier` 目前期待 `sha256`、`batch_id`、`items` 等通用 dict；真实 `PurgeObjectBackupManifest` 使用 `purge_batch_id`、`copies.objects`、`selection.reference_protected` 等结构。现状不能作为生产 verifier。

处置：删除通用 dict 假设，直接解析严格的 `PurgeObjectBackupManifest`；复用 `PurgeObjectRestoreService.verify_copies()`，核对本机 canonical bytes、远端 final manifest、每个 payload、数据库恢复点绑定和保留期，并把 batch/item 快照与 manifest 成员逐字段相等比较。

### B3：没有正式 deleter 和生产删除组合根

当前只有注入 fake 的 `delete_if_present` seam、恒 false 的 `UnavailableFormalDeletionCapabilitySource`，`run_purge_batch_worker.py` 也不组合 `FormalPurgeRepository`/`FormalPurgeWorker`。

处置：新建独立、非日常启动的 formal-delete worker 组合；不得把删除凭证注入 Flask、图片导入 worker、cleanup 或 #26 backup worker。该组合必须使用独立 env/secret 注入、默认不启动、默认 capability false、精确 batch allowlist 和对象/批次上限。没有用户再次授权时，代码即使部署也只能报告关闭。

### B4：正式 OSS 身份与删除原子性信任边界未证明

现有代码没有从 Head 观察贯穿到精确版本/条件删除的生产合同，也没有现场证明正式 Bucket 除全部围栏化写入口外不存在外部覆盖者。

处置：T14 必须二选一并保存证据：

1. 使用经官方接口和隔离演练验证的 exact-version/conditional-delete 机制；或
2. 用部署/IAM 证明正式 Bucket 不允许未受围栏约束的覆盖写，且所有应用写入口都在同一数据库围栏协议内。

两者都无法证明时停止，不能以“Delete 后再 Head”代替竞态防护。

### B5：缺独立、短时、一次性正式删除授权合同

五项安全门和 `backup_only_no_delete` heartbeat 不能升级成删除授权。正式授权证据至少绑定：环境 identity、代码/镜像 digest、batch ID、asset ID 集合、允许的 source/preview 对象上限、数据库恢复点 manifest SHA-256、对象 manifest SHA-256、签发/过期时间、授权引用和操作者。建议有效期不超过 15 分钟，并在首个删除意图原子消费；重启只能恢复同一批次/同一授权范围。

处置：增加独立的 fail-closed capability source。它必须在 claim 前、每次 original Delete 前、每次 preview Delete 前和数据库最终化前重新求值。缺失、撤销、过期或范围不匹配时不发起下一次不可逆调用。

### B6：监控/告警只有代码信号，没有生产闭环

当前有 heartbeat、结构化错误码、批次/检查点和 Compose `restart: unless-stopped`，但 purge worker 没有 healthcheck，也没有已配置的指标采集、告警规则和通知接收方。

处置：在授权环境中接入并演练至少以下告警：每日备份超过 26 小时未成功、五项 gate 非 valid、heartbeat 缺失/failed/过期、batch 阶段超时、`partial_failure`/`PURGE_REPROTECTION_REQUIRED`、held fence 超时、授权撤销后仍有调用、审计写入失败。实际监控产品、规则 ID、接收渠道和确认人必须写入授权记录，不能在计划中猜测。

### B7：生产 schema 状态未知

#27 使用显式迁移，应用启动不会代跑。当前没有证据表明目标环境已具备 item authorization、lease、event、purge fence 与 binding fence 全部列/约束/索引，也没有存量 `pending_deletion` 批次的处置证据。

处置：先为 #27/#28 显式迁移提供受控、幂等、仅 forward 的 CLI 与 dry validation；再取得真实数据库只读 schema 盘点授权。如需迁移，单独取得数据库 schema 写授权、恢复点和回退方案。只做 additive forward migration，禁止自动 down migration；存量不完整批次必须取消/隔离或重新生成完整备份授权，不能回填猜测值。

### B8：证据最大新鲜度没有由消费者强制

当前五项安全门主要相信发布者填写的 `expires_at`；如果发布者给出异常长有效期，消费者没有按 condition 强制最大年龄。pipeline capability 也应明确拒绝过远未来的 `verified_at`。

处置：在消费者中按 condition 强制 `now - verified_at` 和 `expires_at - verified_at` 上限，并保留 60 秒以内的时钟偏差容忍；默认策略使用 daily 26 小时、即时恢复点/副本复验 60 分钟、IAM/对象保护 24 小时、恢复演练 24 小时、worker heartbeat 120 秒。策略改变属于安全边界变更，必须评审并部署；发布者不能通过自行写多年后的 `expires_at` 绕过。

### B9：formal worker 的不可逆调用时序仍只是 fake 合同

当前 worker 先写 delete intent，再用松散参数申请授权，deleter 只接收裸 key；没有把正式对象 Head/版本观察、实际 fence 集与 Delete 调用绑定，异常也主要折叠为通用错误。

处置：明确区分 intent 前 404 与持久 intent 后重放 404；在同一授权对象中携带对象版本/身份观察、operation kind 和实际 fence IDs，让 deleter 只消费该对象；完整维护 original/preview intent/delete/checkpoint 时间和稳定错误码。不能通过“Delete 后再 Head”声称消除了 Head 与 Delete 之间的身份竞态。

---

## 3. F3 / F4 / F5 / N2 处置方案

### F3：终态或保留期到期后 held purge fence 无安全收敛

不能统一“释放所有 fence”：若对象已删而数据库尚未最终化，释放会允许把缺失/错误对象重新绑定。按 checkpoint 分两类处理：

- **尚未发起任何 Delete**：非重试失败进入 `partial_failure` 时，在同一事务释放本 item 的 held purge fences 和资产 reservation，并记录一年期事件。
- **已有 delete intent、对象已删或对象状态未知**：进入明确的 `reprotection_required`/`PURGE_REPROTECTION_REQUIRED` 状态，保留 fence 与 reservation，停止所有后续 Delete，告警并要求人工恢复/复验。只有在对象已从验证副本恢复到受控位置并完成身份复验，或数据库最终化安全完成后，才能以显式事务释放 fence。
- **`deleting` 中保留期到期**：不得继续删除、不得静默永久 held；转为上述可观测的 re-protection 状态并给出唯一 next action。

必测分支：pre-intent nonretryable 释放；original intent 后失败不释放且阻止 bind；re-protection 完成后只释放本 item；worker 重启不重复已完成 Delete；过期批次不再 claim。

### F4：声明 `fence_ids` 只验数量，不与事务内派生集合比较

移除调用者可随意传入的 `{'verified': True}`/松散对象。仓库在同一授权事务中派生实际 original+preview 两把 fence，返回不可伪造的 `DeleteCallAuthorization`，至少含 batch、asset、claim generation、operation kind、formal identities、精确 fence ID 集和短时过期时间。worker/deleter 只能消费该返回值；任一声明值与事务内集合不完全相等、ID 重复/交换、claim 过期或 operation 不匹配都必须零 Delete。

必测分支：伪造两 ID、另一批次的两 ID、过期 token、original token 用于 preview、事务提交后 fence 被替换，均拒绝且 fake deleter 调用数为零。

### F5：正式启用前确保所有 HTTP 正式对象写入口启用 binding fence

当前三个 HTTP factory 已调用 `request_fence_kwargs()`，但 `INGEST_BINDING_FENCE_ENABLED` 默认为关闭，属于部署可选路径。T14 不能只检查一个容器的 env 值：

- 在删除能力可能存在的部署版本中，把 binding fence 变成所有正式写路径的强制合同；生产配置缺失/false 时 backend 启动失败，不得静默退回 legacy 路径。
- 盘点并测试 `/api/products` POST/PUT、`/api/image-imports`、同步 `/api/image-assets/import`；异步 promotion 和 cleanup 继续保持围栏；任何仍可写正式 OSS 的迁移/运维入口在删除窗口内必须停用或采用同一围栏。
- 先完成 schema 迁移和“围栏开启、删除关闭”的部署，观察至少一个完整业务周期并确认零异常 held lease，再讨论 formal-delete worker。
- formal-delete worker 的 preflight 必须验证部署版本/配置证据，不能只相信人工口头确认。

### N2：缺 HTTP 层“已提交 existing + 新图混合 + 围栏开启”专项测试

在 `backend/test/integration/test_issue_27_product_ingest_boundary.py` 增加 Product POST/PUT 与 import queue 用例：第一张命中已提交 existing asset/import item，第二张为新图；断言请求成功、existing 不重复写/不新建错误 fence、新图在外层 commit 后原子绑定、共享 preview 无自锁、失败回滚后零 held fence。该测试是本地代码门，不需要真实环境授权，必须在 F5 部署前完成。

---

## 4. 逐项授权清单（对应 Issue #28 验收标准）

每项执行前先展示授权记录并停下来取得用户明确答复。一个“同意继续”只覆盖记录中列出的一个授权 ID。

每条授权记录至少包含以下字段：

- `authorization_id`、Issue/变更单引用、签发人、操作者、独立复核人；
- `environment_name` 与不可变 identity、云账号/区域、数据库脱敏 identity；
- formal/backup/isolation Bucket 别名及允许前缀，禁止写入凭证或签名 URL；
- 允许动作的精确集合（read、Put-if-absent、deploy、schema migration、restore、start/stop、Delete）和明确排除项；
- 允许的 batch IDs、asset IDs、最大批次数、最大资产数、最大对象调用数；
- UTC `valid_from`/`valid_until`、代码/镜像 digest、数据库与对象 manifest SHA-256；
- 证据落点、停止条件、快速关闭责任人，以及资源后续处置需要的新授权 ID。

### AC1：开始前明确环境和范围

**需要的授权内容**

- `AUTH-28-01`：允许读取指定环境的部署、调度、数据库 identity、Bucket/IAM/生命周期和证据目录状态。
- 明确环境名称与不可变 identity、云账号/区域、正式/备份/隔离 Bucket 别名、数据库 system identity、允许的操作类别；默认只读。
- 写入、部署、迁移、恢复、启动服务和删除均不包含在 `AUTH-28-01`。

**执行步骤**

1. 两人核对环境 identity 与授权记录完全一致。
2. 盘点所有角色、Bucket、数据库、worker、证据发布者和监控接收人。
3. 发现身份不唯一、凭证复用或无法确认即停止。

**证据要求**

- 带 UTC 时间的授权记录、脱敏 identity、操作者和复核人。
- 不记录密码、DSN、secret、签名 URL、图片内容或向量值。

### AC2：每日 PostgreSQL 全量备份、30 天与双副本

**需要的授权内容**

- `AUTH-28-02R`：只读检查调度历史、本机 backup root、远端 backup Bucket Head/Get、最近连续 30 天 final manifest。
- 如需补跑，另设 `AUTH-28-02W`：允许对明确 backup prefix 执行一次 `create-daily` 的数据库只读 dump、本机文件写和备份 Bucket Put-if-absent；不含 Delete/覆盖。

**执行步骤**

1. 检查最近 30 个 daily ID 是否按批准时区连续，并核对调度失败/重试历史。
2. 对最新及抽样历史 manifest 执行 `verify-copies`；核对本机与异机 SHA-256。
3. 现场核对 lifecycle/Object Lock 等保护实际覆盖对应前缀至少 30 天。

**证据要求**

- 调度规则 ID、30 日成功清单、异常处置记录、manifest SHA-256、双副本复验结果、保留策略 ID。
- 任一天缺失、任一副本失败、只有 attempt JSON 或 manifest 自称 production gate 时结论为 failed。

### AC3：即时恢复点与非生产数据库恢复演练

**需要的授权内容**

- `AUTH-28-03A`：创建一个精确 batch ID 的即时恢复点；允许源库一致只读、备份本机/远端写入，不含恢复。
- `AUTH-28-03B`：把该异机副本恢复到指定 disposable PostgreSQL 16 + pgvector 实例；允许创建程序生成的新数据库，不允许覆盖既有库，不允许 drop。

**执行步骤**

1. 用授权记录 `AUTH-28-03A.batch_id` 的精确值运行 `create-restore-point --purge-batch-id`，并立即 `verify-copies`；不得临场改用另一批次。
2. 第二次核对恢复目标 host/port 与源地址不同、`system_identifier` 不同、`RESTORE_VERIFY_DISPOSABLE=1`，再运行 `verify-restore --acknowledge-isolated`。
3. 核对 `vector` extension、`products`、`image_assets`、`vector(1024)`、商品/资产行数、非空向量数、embedding model/dimension 分布；只保存计数与摘要哈希，不保存完整向量。
4. 保留恢复数据库供复核；其后处置另行授权。

**证据要求**

- 填写 `docs/operations/templates/postgresql-restore-drill-record.md` 的安全副本；记录 backup/batch ID、manifest SHA-256、目标数据库名、结构与计数结果、开始/结束时间和双人复核。
- 任何地址/system identity 相同、PG major 非 16、向量维度不为 1024、行数/摘要不符都立即失败关闭。

### AC4：备份 Bucket 加密、生命周期和最小权限

**需要的授权内容**

- `AUTH-28-04`：只读查看指定 Bucket ACL、默认加密/SSE、生命周期/Object Lock、IAM policy/version 和 access-key identity；不调用 Delete。
- 若平台只支持实际权限探测，必须再申请隔离 sentinel 的独立授权，绝不能拿真实备份对象做 Delete 探针。

**执行步骤**

1. 核对正式、备份、隔离 Bucket 三者不同，四类凭证 identity 不复用。
2. 用策略文档/策略模拟器确认备份角色仅指定前缀 Put-if-absent/Head/Get，无 Delete/ACL/lifecycle；应用角色不能访问备份 Bucket。
3. 核对 private、SSE 和至少 30 天保护实际生效。

**证据要求**

- 脱敏 policy ID/version、Bucket 配置版本、只读截图或审计导出、复核时间与人员。
- 仅凭代码窄接口、`.env.example` 或 manifest 的 `not_verified` 字段不能通过。

### AC5：隔离对象备份、哈希和恢复演练

**需要的授权内容**

- `AUTH-28-05A`：在明确的非生产数据库和 formal-like 源 Bucket 中创建/选用测试资产，并通过受控批次写备份 Bucket；列明最大资产数、最大对象数和前缀，不含任何 Delete。
- `AUTH-28-05B`：把该 manifest 恢复到指定隔离 Bucket/派生前缀；允许 Put-if-absent/Head/Get，不含正式 Key 写入或 Delete。

**执行步骤**

1. 证明测试资产不属于未授权生产资产，创建 batch 并让 #26 流水线生成 plan/payload/final manifest。
2. 运行 `verify-copies`，核对 source/backup 大小、SHA-256、batch 绑定和保留期。
3. 使用唯一 restore run ID 运行 `restore-isolated --acknowledge-isolated`，核对派生 Key、写后 Head 和重新下载 SHA-256。
4. 确认没有写回正式 Key、没有调用 Delete；保留隔离对象等待另行处置。

**证据要求**

- 填写 `docs/operations/templates/purge-object-restore-drill-record.md` 的安全副本；记录环境、batch、restore run、两个 manifest SHA-256、对象计数/字节数、哈希结论和双人复核。

### AC6：管理员认证、worker 重启、监控、检查点和告警

**需要的授权内容**

- `AUTH-28-06`：在指定隔离环境部署/启动测试 worker，执行一次受控 SIGTERM/重启和告警演练；明确监控平台、告警接收渠道和允许制造的失败类型。

**执行步骤**

1. 验证未配置/缺失/错误管理员 token 被拒绝，正确 token 只暴露脱敏 DTO。
2. 分别在 database backup、object backup/verifying 和 fake formal checkpoint 中断 worker，确认 lease 到期/重启后从持久状态续跑，不覆盖副本、不重复已完成 Delete。
3. 验证 heartbeat 120 秒内有效、停止后过期关闭；触发并确认备份超时、gate 过期、worker unhealthy、partial failure、held fence 和审计失败告警。
4. 保存批次事件、item checkpoint、claim generation 和告警确认时间；不保存对象 Key 或秘密。

**证据要求**

- 重启前后 worker instance ID、batch/item 状态、checkpoint、告警规则 ID、通知送达与人工确认记录。
- 只有 Compose restart policy 或本地单测，不能满足现场验收。

### AC7：带时间/环境/批次身份的证据与失败关闭

**需要的授权内容**

- `AUTH-28-07`：允许独立 evidence publisher 向指定 `PURGE_GATE_EVIDENCE_DIR` 发布五个脱敏 JSON；backend/worker 继续只读。

**执行步骤**

1. 完整证据保存在受控、不可变审计位置；运行目录只发布摘要和引用。
2. 发布前核对五项 condition、UTC `verified_at/expires_at`、环境和 batch 绑定。
3. 逐个模拟缺失、过期、failed、坏 JSON 和敏感键，确认 gate 关闭；恢复有效文件后重新评估。
4. 按 B8 在消费者侧强制最大 freshness：daily 26 小时；即时恢复点/副本复验 60 分钟；IAM/对象保护 24 小时；恢复演练 24 小时；worker heartbeat 沿用 120 秒。这些值必须写入批准的运行策略，发布者不能自行放宽。

**证据要求**

- 五个运行 JSON 的摘要哈希、发布者 identity、文件权限/挂载证据和 API readiness 截图/响应摘要。
- 能写 gate 目录者等同能让基础 gate 报 ready，必须记录为主机信任边界。

### AC8：受控部署、先关闭验证、人工启用与快速关闭

**需要的授权内容**

- `AUTH-28-08D`：部署已通过 risk review 的代码/显式 additive migration，但不注入删除凭证、不发布 formal authorization、不启动 formal-delete worker。
- `AUTH-28-08E`：仅在所有前置证据通过后，允许为精确环境/批次注入短时删除凭证和 formal authorization；该授权仍不自动包含真实试删，除非与 AC9 的精确范围同时明确写入。

**执行步骤**

1. 两阶段部署：先 schema/围栏，后 formal-delete 代码；每阶段删除能力均保持 false。
2. 部署后主动验证：缺 formal evidence、过期 evidence、无凭证、围栏未开启、监控不可用时均为关闭，且零 claim/零 Delete。
3. 配置独立 formal-delete worker 和 read-only authorization mount；不把删除 secret 注入现有服务。
4. 启用前演练快速关闭：原子撤销/失效 formal evidence → 停止 formal-delete worker → 吊销删除凭证 → 确认没有下一次 Delete。

**证据要求**

- 镜像 digest、schema version、配置摘要、关闭态 preflight、零 Delete 审计、关闭演练耗时和凭证吊销确认。

### AC9：真实永久清除试运行再次授权

**需要的授权内容**

- `AUTH-28-09` 必须在试运行前新签发，列明唯一环境、唯一 batch ID、精确 asset ID、source/preview 最大 Delete 数、最大批次数、有效期、操作者、复核人和立即关闭条件。
- 默认建议第一轮不超过 1 个 batch、1 个 asset、最多 2 个对象调用；这只是建议，不是授权。

**执行步骤**

1. 在授权窗口内再次复验数据库恢复点、对象 manifest/副本/保留期、引用、对象身份、围栏、监控和 formal capability。
2. 先观察 one-shot worker 的精确 scope 输出，第二人确认后才允许进入第一个 delete intent。
3. 每次 Delete 前重新求值授权；达到数量上限、证据过期或任一异常立即关闭。
4. 批次结束后立即撤销 formal evidence、停止 worker、吊销凭证，保存一年期审计。

**证据要求**

- 授权原文引用、batch/item checkpoint、删除调用计数、对象身份校验结果、数据库最终化、向量不可检索、关闭时间和异常/恢复记录。

---

## 5. 恢复演练计划

### 隔离边界

- 数据库恢复目标必须是独立 disposable PostgreSQL 16 + pgvector，地址和 `system_identifier` 均与源不同，不挂接 backend/frontend/worker，不允许入站业务流量。
- 对象演练使用非生产 formal-like 源 Bucket、独立备份 Bucket和独立隔离恢复 Bucket；三者 identity 不同，凭证互异。
- 恢复工具只能派生隔离 Key；操作者不能提交正式目标 Key。
- 演练使用真实备份/恢复实现，但不包含任何 Delete、覆盖、正式 Key 写回、数据库 drop 或资源清理。

### 顺序与回退点

1. **R0 授权前：** 只做本地检查；未产生外部状态，可直接停止。
2. **R1 身份预检：** 只读核对 DB/Bucket/IAM/调度；任一 identity 不明确即停止。
3. **R2 数据库备份：** 以 `purge-` 加授权 batch ID 形成稳定恢复点 ID 并复验双副本；失败时保留 partial/orphan，同 ID reconcile，不覆盖、不删除。
4. **R3 数据库恢复：** 恢复到程序生成的新库并核对商品、资产、向量；失败时保留目标库和证据，隔离网络，不自动 drop。
5. **R4 对象备份：** plan → payload → final manifest；任何引用变化、哈希冲突或保留期异常都停止，不发布 final manifest。
6. **R5 对象隔离恢复：** 备份读回 → 隔离 Put-if-absent → Head/下载哈希；失败时保留隔离对象，不覆盖、不删除。
7. **R6 worker 重启：** 在隔离环境中断并恢复；确认 checkpoint/lease/idempotency 后才继续。
8. **R7 证据发布：** 发布五项 gate 摘要，逐项验证未知/过期/失败关闭；完整证据留在受控审计位置。
9. **R8 演练结束：** 关闭恢复开关、撤销临时凭证、停止测试 worker。数据库和对象后续清理由新的精确授权处理。

### 恢复成功标准

- PostgreSQL final manifest 和对象 final manifest 都是 canonical、complete、同 batch、未过期；本机/异机/隔离下载 SHA-256 一致。
- `products`、`image_assets` 行数符合证据，`vector` extension 存在，列为 `vector(1024)`，非空向量计数与模型/维度分布匹配。
- 没有正式 Key 写入、没有 Delete、没有凭证/DSN/签名 URL/向量泄露。
- worker 重启后没有重复不可逆动作，告警真实送达并被确认。

---

## 6. 快速关闭入口与残余风险

正式删除启用设计必须提供以下顺序的关闭入口，并在隔离环境计时演练：

1. evidence publisher 原子发布 `failed/revoked` 或移除精确 formal authorization；worker 在下一次外部调用前重新读取并拒绝。同时由 IAM 操作者禁用/吊销独立删除凭证，形成进程内与云侧两道关闭。
2. 只停止独立 formal-delete worker；不要停止每日备份 worker，也不要删除 capability/审计/manifest。
3. 不以删除/过期五项基础 gate 文件作为主要 kill switch：当前 cancel/retry 也要求基础 gate ready，错误地关闭基础 gate 可能妨碍处置；正式删除必须有独立 capability。
4. 查询数据库确认没有活 claim 或记录最后 checkpoint；若 Delete 已发起，按“状态未知”处理，不能猜测成功/失败。
5. 保持 purge/binding fence 和资产 reservation，直到对象/数据库状态经另行授权的恢复与复验收敛。

残余风险必须向授权人明确：

- 关闭信号与云端 Delete 之间存在不可消除的在途窗口；快速关闭不撤销已接受的云请求。
- 当前仓库没有正式 Key 写回恢复工具。已删对象只能先恢复到隔离位置；写回正式位置和数据库恢复必须重新授权并使用尚待设计/演练的受控流程。
- 整库恢复可能回退清除批次之后的合法业务写入；生产事故恢复需要明确 RPO/切换方案，不能直接把非生产演练命令指向生产库。
- 因此在生产写回恢复方案、正式 OSS 信任边界和 F3/F4/F5 未闭合前，AC9 必须保持 NO-GO。

---

## 7. 预计代码与文档变更范围（后续实施，不属于本轮授权）

| 文件 | 计划责任 |
| --- | --- |
| `backend/services/formal_purge.py` | 真实 manifest verifier、精确 `DeleteCallAuthorization`、每次调用前 capability 复核、正式对象身份时序、F3 收敛 |
| `backend/services/purge_batch_control.py` | canonical manifest → item authorization 原子提升、re-protection 状态/next action |
| `backend/models/purge_batch.py`、`backend/models/purge_item_event.py` | 如 F3 需要，增加明确可观测状态/事件；保持墓碑和一年审计 |
| `backend/migrations/issue_28_controlled_enablement.py`、`postgres/init/01_init.sql` | 仅 additive schema；显式执行，绝不随启动隐式迁移 |
| `backend/services/purge_formal_deletion_capability.py` | 批次绑定、短时、一次性、可撤销的 fail-closed authorization source |
| `backend/services/purge_object_storage.py` | 独立删除角色和 exact-version/受信任边界合同；不增加 Put/List/批量 Delete |
| `backend/scripts/run_formal_purge_worker.py` | 独立 one-shot/allowlisted 生产组合；与 #26 worker 隔离 |
| `backend/services/fence_composition.py`、三个图片写蓝图 | F5：删除可用部署中 binding fence 强制开启 |
| `docker-compose.yml`、`backend/.dockerignore`、env examples | 默认不启动的 formal-delete profile、只读 evidence mount、删除 secret 不进其他服务/build context |
| `backend/test/test_issue_28_*.py`、`backend/test/integration/test_issue_28_*.py` | B1–B9、F3/F4/F5、快速关闭、重启和零真实云访问合同 |
| `backend/test/integration/test_issue_27_product_ingest_boundary.py` | N2 existing+new HTTP 混合回归 |
| `docs/operations/*` 与模板 | 正式授权、监控、关闭、re-protection 和演练证据说明 |
| `AGENTS.md` | 仅在架构事实、入口或操作约束实际改变后更新 |

后续完整 diff 必须由 `risk_reviewer` 独立审查；审查通过只说明代码可进入下一授权门，不授权部署或真实环境动作。

---

## 8. 阶段门与最终判定

| Gate | 通过条件 | 当前状态 |
| --- | --- | --- |
| G0 本地代码 | B1–B9、F3/F4/F5/N2 全部实现并定向/全量测试通过 | **LOCAL COMPLETE / PENDING G1** |
| G1 架构与风险审查 | architect + risk_reviewer 对完整 diff 批准 | **APPROVE（仅进入 G2 授权门）** |
| G2 真实只读盘点 | AC1/AC2R/AC4 明确授权且证据通过 | **WAITING AUTHORIZATION** |
| G3 隔离恢复演练 | AC3/AC5/AC6 授权，数据库与对象演练、重启和告警通过 | **WAITING AUTHORIZATION** |
| G4 证据发布 | AC7 授权，五项 fresh，失败关闭已现场复验 | **WAITING AUTHORIZATION** |
| G5 硬关闭部署 | AC8D 授权，部署后 formal deletion 仍 false、无删除凭证 | **WAITING AUTHORIZATION** |
| G6 受控启用 | AC8E 授权，短时 batch-bound evidence 和独立凭证就绪 | **WAITING AUTHORIZATION** |
| G7 真实试删 | 新的 AC9 精确授权，范围/数量/窗口再次确认 | **WAITING AUTHORIZATION** |

**Architect review（2026-08-31）：NO-GO。** 审查确认当前 HEAD 只能继续本地补码、离线测试，以及逐项授权后的备份/隔离恢复演练；不能进入真实删除启用、删除凭证注入或试删。审查发现的生产组合、授权快照、manifest schema、真实 deleter、F3/F4/F5、freshness、监控和 schema 盘点问题均已纳入 B1–B9 与阶段门。

**当前最终判定：NO-GO。** 2026-09-05 已补完 G0 盘点中的本地代码缺口并完成定向回归；下一步是 G1 对完整未提交 diff 做 risk review，或申请某一条只读/隔离环境授权。不得把本地测试通过解释为 G5–G7 授权。

## Progress

- **Progress（2026-08-31，B1 seam 1）：** 新增 `services/purge_formal_authorization.py` 作为唯一 typed canonical manifest → formal authorization bundle seam。它通过 `PurgeObjectBackupManifest.from_dict()` 重新执行严格 manifest 合同，拒绝无效 SHA-256、naive 时间、过期保留期、成员缺失和正式 Bucket 不一致；输出不可变 batch/item 授权 DTO，并对同批共享 preview 只把确定性最后一个 asset 标为删除 owner。首个 tracer test 先因模块不存在 RED，最小实现后 `test_complete_manifest_builds_one_typed_item_authorization` **1 passed**。未接 worker、未组合 deleter/凭证，生产删除仍硬关闭。
- **Progress（2026-08-31，B1 seam 2）：** `PurgeBatchControlService.advance_verified_to_pending_if_current()` 已删除松散 dict 接口，只接受 `FormalPurgeAuthorizationBundle`；在同一事务锁住 verifying batch、全部 items 与 archived assets，精确核对 manifest digest、保留期、完整资产集合、正式 Bucket、original/preview 身份与可删 preview 备份字段，再一次性写入授权快照并进入 `pending_deletion`。`run_purge_batch_worker.handle_verifying()` 读取本机 manifest bytes、复验副本/当前候选、构建 typed bundle 后调用专用提升入口，不再返回通用 `'pending_deletion'` 让 `advance_if_current()` 绕过快照。两条 tracer 均先 RED 后 GREEN；B1 定向回归 **22 passed**。未执行迁移、未启动 worker、未访问对象存储。
- **Progress（2026-08-31，B2）：** `CanonicalFormalPurgeAuthorizationVerifier` 已删除测试用通用 dict schema，只接受 typed `VerifiedPurgeObjectBackup`；它按 UTC 复核 batch/item 保留期、manifest digest、copy verifier 结果，随后调用唯一 bundle seam 并逐字段比较 batch、asset、formal Bucket、original/preview backup 身份与 preview owner。未知 stage、类型、摘要、副本或任一快照差异都 fail closed；非 preview owner 不能取得 preview operation。allow 与三类 deny tracer 均先 RED 后 GREEN，定向 **2 passed**。真实 copy adapter 尚未组合，生产删除保持硬关闭。
- **Progress（2026-08-31，B3）：** 新增独立 one-shot `scripts/run_formal_purge_worker.py`；`run_one_shot()` 在 capability false/异常时先返回 disabled，绝不构造 worker、repository 或对象客户端。Compose 增加默认不启动的 `formal-delete` profile，`restart: "no"`、`PURGE_FORMAL_DELETION_ENABLED=0`，不加载应用、`.env.backup`、formal env 或任何 `PURGE_DELETE_OSS_*`。入口当前只组合恒 false source，仍不具备真实删除能力。组合顺序与 Compose 静态 tracer 均先 RED 后 GREEN，定向 **2 passed**。
- **Progress（2026-08-31，B4 trust seam）：** 新增 `services/purge_delete_trust.py`，只接受裁决确定的 `fenced_writers_iam_no_overwrite` 模式；typed attestation 精确绑定 environment、formal Bucket、IAM policy SHA-256 与完整 writer inventory SHA-256，消费者强制 24 小时最大窗口、60 秒未来时钟容忍和 canonical attestation digest。scope、过期或 exact-version 等未授权模式一律返回 `PURGE_NO_OVERWRITE_TRUST_INVALID`/拒绝。两条 tracer RED→GREEN，定向 **2 passed**；未实现 exact-version、未创建 OSS client、未执行 Delete。
- **Progress（2026-08-31，B5）：** `purge_formal_deletion_capability.py` 已扩展为与五项 backup gate 完全独立的 typed grant source。JSON 采用 exact-schema、64 KiB 上限与递归敏感键拒绝，最长 15 分钟，精确绑定 environment/deployment、单一 batch、排序 asset 集、数据库/对象 manifest SHA-256、formal Bucket、最多 40 次对象调用及 B4 trust attestation；disabled、缺失、scope/count/digest/时钟不匹配一律返回 unavailable。one-shot root 在构造 worker 前传入精确 context；`FormalPurgeWorker` 在 original intent/授权/Delete、preview intent/授权/Delete 与数据库最终化前均重新求值 capability，撤销后错误码为 `PURGE_FORMAL_DELETION_DISABLED` 且零后续 Delete。三组 tracer RED→GREEN，相关定向 **10 passed**；当前 main/Compose 仍只组合恒 false source。
- **Progress（2026-08-31，B6）：** 新增 vendor-neutral `formal_purge_observability.py`：独立 formal health evidence 采用 exact-schema、原子 0600 写入、120 秒 TTL 和 typed snapshot；`FormalPurgeOperationalEvent` 只允许 event/environment/batch/asset/checkpoint/result/error code 固定字段，JSON sink 无自由 payload，不能携带 Bucket/Key/请求体/secret。新增只读 `scripts/check_formal_purge_health.py`，输出稳定脱敏 JSON 与 0/2 退出码，供任意监控平台采集；未接厂商 SDK/通知渠道。三条 tracer RED→GREEN，定向 **3 passed**。
- **Progress（2026-08-31，B7）：** 新增 `PurgeSchemaManager` 的稳定 #27 migration plan digest、typed schema snapshot/check 和关键表/授权列/held partial-unique index 缺失清单；apply 必须同时匹配已审 plan SHA-256 与 `acknowledge_additive=true`。`manage_purge_schema.py` 提供 `plan/check/apply`：plan 完全离线，未确认 apply 在构造数据库连接前拒绝，运行异常只输出脱敏稳定码。三条 tracer RED→GREEN，定向 **3 passed**；本轮未运行 check/apply、未连接或迁移任何真实数据库。
- **Progress（2026-08-31，B8）：** 五项 `PurgeSafetyGate` 由消费者强制 condition-specific 最大 age/lifetime：daily 26 小时、即时恢复点 60 分钟、对象保护/独立凭证/恢复演练 24 小时；发布者即使写更远 `expires_at` 也只会得到 failed/expired。`FilePurgePipelineCapabilitySource` 同时拒绝超过 60 秒未来偏差的 heartbeat。两条 tracer 均先 RED 后 GREEN，gate/capability 定向 **17 passed**。
- **Progress（2026-09-01，B9 / F4）：** 删除松散 `verified_authorization`/caller-declared fence IDs 接口；`FormalPurgeRepository.authorize_delete_call()` 只接受 typed `FormalObjectObservation`，在同一事务内派生、锁定并提交实际 original+preview 两把 fence，返回绑定 batch/asset/claim generation/operation/formal identity/精确 fence IDs/当前字节观察/短期 lease 的 `DeleteCallAuthorization`。新增未组合的 `OssFormalObjectDeleter` 测试 Adapter：Head+Get 重算 SHA-256，Delete 前再次观察并拒绝 ETag/size/hash 变化，Delete 后 Head 必须缺失，唯一成功输出为 typed `DeletionObservation`。worker 在 intent 前先观察，intent 前 404 用 `PURGE_ORIGINAL_MISSING_BEFORE_INTENT`/preview 对应码失败；只有持久 intent 重放才把 404 解释为 already-absent。original/preview intent/delete 时间戳字段随 checkpoint 写入。唯一 `.delete_object()` 原语静态合同限定在 `purge_object_storage.py` 且恰一处，无 `from_env`、未被组合根导入。repository/worker/deleter/多会话定向均已回绿。
- **Progress（2026-09-01，F3）：** 新增 `reconcile_expired_authorizations()`：过期且尚未进入 delete intent 的项释放 held fence、标为 `reprotected/PURGE_BACKUP_RETENTION_EXPIRED`；已有 intent/状态未知的项停止推进、清 claim、进入 `PURGE_REPROTECTION_REQUIRED` 并保留 fence/资产预约。`confirm_reprotected()` 仅在 archived asset 绑定未变且 original/需删除 preview 的 Bucket、Key、SHA-256 全部与备份快照一致时释放 fence、追加一年事件并标记 reprotected；回收站恢复与新批次 reservation 谓词只豁免该明确结果。两条 PostgreSQL tracer 与恢复合同 RED→GREEN，F3 定向 **5 passed**。
- **Progress（2026-09-01，F5）：** `fence_composition.py` 固定五类 formal writer inventory 并生成稳定 SHA-256；`PURGE_FORMAL_DELETION_DEPLOYED=1` 时任一 HTTP factory 未显式开启 `INGEST_BINDING_FENCE_ENABLED` 立即失败，不能回退 legacy。formal grant source 还要求 trust attestation 的 writer inventory digest 与本地值完全一致。scope/inventory 两条 RED→GREEN，相关定向 **4 passed**；Compose 仍未设置 deployed 标志，正式删除硬关闭。
- **Progress（2026-09-01，N2）：** 新增 Product PUT 与持久 import queue 的 HTTP existing+new 混合场景：已提交 existing 的来源身份/向量/任务行不变且不重复 PUT，新图正常绑定，整请求无自锁、最终零 held fence。该 finding 原本即标注“代码层已证安全、缺专项用例”，因此两条 coverage tracer 首次运行即 GREEN，定向 **2 passed**。
- **Risk review（2026-09-01，首轮）：REJECT。** 默认生产删除硬关闭通过；阻塞项为 intent 与 fence 分事务崩溃窗口、grant 未持久消费/未扣调用上限、DTO 提交后缺 executing 前置重验、schema check 假阳性，以及恢复点绑定/F5 启动门/B6 未接入等。用户裁决采用两表持久 grant/permit + 原子 `begin_delete_intent` + executing 前置重验方案。
- **Repair progress（2026-09-01，持久 grant/permit schema）：** 新增 `FormalDeletionGrantConsumption` 与 `FormalDeleteCallPermit` ORM；grant ID 与 batch 均唯一，permit 对 `(batch_id,target_asset_id,operation_kind)` 唯一，状态/时间/计数/调用上限由 CHECK 约束，审计至少一年。新增独立 `issue_28_formal_delete_permits.py` additive migration，并同步首次初始化 SQL、models export、静态合同和真实 PostgreSQL uniqueness 测试。RED 后 schema 合同 **2 passed**；未执行迁移。
- **Repair progress（2026-09-05，G0 盘点后三路补洞）：** 只读盘点确认 REJECT 主干（原子 `begin_delete_intent`、两表 grant/permit、Flask F5 启动门）已在工作区；剩余本地缺口分三路 TDD 补完，未 commit、未 apply 迁移、未访问 OSS。
  - Pane A：`CanonicalFormalPurgeAuthorizationVerifier` 必填 `restore_point_loader`，活复验 `kind=purge_restore_point` 的 backup_id/SHA/`retain_until`；`start_delete_call` 锁 permit 后再跑 `manifest_validator` 且拒绝交换 fence_ids/过期 DTO；`fail(retryable=False)` 按 checkpoint 释放或 `PURGE_REPROTECTION_REQUIRED`；worker 在 claim 前调用 `reconcile_expired_authorizations`。
  - Pane B：UNIQUE 显式名 `uq_formal_delete_permit_item_operation` 对齐 migration/`01_init.sql`/ORM；`check()` expected 来自 migration SQL catalog，不再用 ORM `create_all` 自证。
  - Pane C：`run_one_shot` 在 capability 前校验 deployed+fence 合取与 writer inventory digest；`main()` 仍只组合恒 false source；Compose healthcheck 保持 `disable: true`（one-shot + `restart: no` 下启用 probe 会逼改重启边界）。只读探针仍是 `check_formal_purge_health`。
  - 主线程合流定向：`107 passed`（issue 27/28 formal/schema/worker/gate）+ `29 passed`（相关 26/27 worker 与 N2 HTTP）。生产删除仍硬关闭。G2–G7 仍等授权。
- **Risk review（2026-09-05，G1）：APPROVE。** 只读审查完整未提交 diff：生产删除硬关闭 PASS；REJECT 七项代码合同闭合；backup/gate/health 未被升级成删除授权。独立复跑 T14 unit `44 passed`、formal repository + permit schema 集成 `33 passed`。本批准只表示可进入 G2 只读盘点授权门，不等于部署、schema apply、注入删除凭证或试删。G6 接线时禁止 restore-point loader 回显已落库 SHA；不得只靠 `PURGE_FORMAL_DELETION_ENABLED=1` 打开删除。审查原文：`/tmp/t14-g1-review.md`。
