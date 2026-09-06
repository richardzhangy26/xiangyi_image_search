# PostgreSQL 恢复点、异机备份与恢复验证设计

## 目标与边界

本设计实现 Issue #24 的运维能力：显式创建每日 PostgreSQL 全量备份、按永久清除批次创建即时恢复点、在本机与独立加密私有备份桶保留副本，并通过隔离 PostgreSQL 恢复检查证明备份具备可恢复性。

本交付不创建真实备份桶、不配置真实凭证、不部署、不接入应用启动或健康检查、不启用永久清除，也不执行生产或本地现有 PostgreSQL 的真实备份/恢复。自动化单元测试使用伪命令执行器和伪存储；另提供必须显式配置 disposable pgvector PostgreSQL 才能运行的集成测试入口。本轮不运行该入口，因此真实 custom archive 恢复仍是 production gate，而不是已完成证据。

实施基线是现有提交 `088bb9f`（`refactor/image-search-pgvector`）。当前 Codex worktree 保持 detached，不创建或移动分支引用。

## 备选方案

### 方案 A：独立 Python 领域模块、显式 CLI 与可注入适配器（采用）

备份编排、清单、失败模型、PostgreSQL 命令执行和备份存储分离。CLI 只在运维人员显式调用时运行，依赖通过接口注入，因此可以使用 fake runner/fake storage 完整验证控制流，同时保留对 disposable PostgreSQL 的真实恢复入口。

优点是稳定 JSON 契约、失败阶段清晰、容易测试、能复用仓库现有 `main(argv, deps)` 和原子报告模式；代价是代码量高于单个 Shell 脚本。

### 方案 B：纯 Shell 脚本

可以直接组合 `pg_dump`、`pg_restore` 和 OSS CLI，但结构化清单、可重入失败恢复、凭证脱敏和伪造测试都较脆弱，不采用。

### 方案 C：接入 Flask 或永久清除 worker

可以立即由应用触发恢复点，但会把 T10 耦合到尚未交付的认证、worker 和永久清除状态机，也增加应用进程获得备份凭证的风险，不采用。本交付只提供后续 worker 可调用的稳定 CLI/服务结果。

## 组件与职责

### `services/postgres_backup.py`

负责领域模型和本机工作流：

- 生成稳定且不可变的备份标识。每日备份使用显式 `BACKUP_DAILY_TIMEZONE`（固定默认 `Asia/Shanghai`）中的业务日期 `daily-YYYY-MM-DD`；即时恢复点严格使用 `purge-<purge_batch_id>`。调用方可以用同一标识重试，但不能把同一标识绑定到不同种类、批次或数据库身份。
- 只从专用 `BACKUP_DB_*` 读取源数据库配置，不回退 `DATABASE_URL` 或应用 `DB_*`。密码只进入子进程环境，不进入 argv、JSON、日志或 manifest。
- 使用 PostgreSQL 16 client 执行 custom-format `pg_dump`。启动前检查 `pg_dump`、`pg_restore`、`psql`、`createdb` 的 major version，并读取源 server major；不兼容时稳定失败。
- 在同一文件系统的 0700 staging 目录写 0600 临时 dump，刷新并 `fsync`，再用 `pg_restore --list` 验证可读性，计算大小与 SHA-256，最后原子发布本机 dump。
- 创建版本化、脱敏的候选 manifest，但只有异机 dump 已按 SHA-256 验证后，才把 final manifest 作为远端和本机的最后提交标记。failed/partial attempt 只写独立 attempt result，不能被后续永久清除任务当成 complete 恢复点。
- 已存在同标识时只允许 reconcile：本机 dump、remote dump、final manifest 全部与不可变字段和哈希一致即可返回同一 complete 结果；任何不一致均以稳定冲突失败，绝不覆盖。

### `services/backup_storage.py`

定义只包含 Put、Head、Get 的备份存储协议，并提供专用 OSS 实现：

- 只读取 `BACKUP_OSS_*`，不回退应用 `OSS_*`，并拒绝备份桶与 `OSS_BUCKET_NAME` 相同、备份 access key 与应用 access key 相同。
- 远端对象键严格限定在专用前缀和已校验 backup ID 下。
- 上传强制 `x-oss-forbid-overwrite=true`、私有 ACL 与 SSE；适配器不暴露 Delete、ACL 修改或生命周期管理能力。
- dump 上传后通过下载到 0600 临时文件重新计算 SHA-256。只有哈希一致才允许写 final manifest；随后重新读取 manifest 并核对内容。
- 对已存在对象只允许按大小、SHA-256 metadata 和实际下载哈希复用；不一致即冲突。

真实最小权限仍需在外部 IAM 中验证：备份进程凭证仅允许指定前缀的 Put/Head/Get，无 Delete、ACL 和生命周期权限；应用运行凭证不得访问备份桶。代码与 fake 测试不能替代这项证据。

### `scripts/manage_postgres_backups.py`

提供四个显式子命令：

- `create-daily`：创建或 reconcile 当日全量备份。
- `create-restore-point --purge-batch-id <id>`：创建与清除批次稳定绑定的即时恢复点。
- `verify-copies --manifest <path>`：按 final manifest 重新校验本机和异机副本。
- `verify-restore --manifest <path> --acknowledge-isolated`：只从异机副本下载 dump，并在隔离 PostgreSQL 中创建全新验证数据库后恢复和检查。

退出码固定为：`0` 成功、`2` 配置或用法错误、`3` dump/完整性错误、`4` 存储错误、`5` 恢复或安全门错误。结果使用稳定 JSON，错误只包含 error code、stage 和脱敏摘要，不透传外部工具原始退出码或可能含凭证的输出。

### 恢复验证器

恢复验证使用独立 `RESTORE_VERIFY_DB_*` 管理连接，不能复用生产备份角色：

- 必须提供显式 acknowledgement，并要求配置声明该实例为 disposable；缺一即拒绝。
- 验证源数据库脱敏身份与隔离实例身份不同。
- 目标名只能由程序生成，格式为 `backup_verify_<随机十六进制>`；不接受调用方提供已有数据库名。
- 先查询目标不存在，随后 `createdb --template=template0`；目标存在即失败。
- `pg_restore` 固定使用 `--exit-on-error --single-transaction --no-owner --no-acl`，禁止 `--clean` 和 `--create`。
- 自动化绝不执行 `dropdb`。成功或失败数据库均保留，由隔离环境自身回收；结果给出脱敏目标名供演练人员处置。
- 检查 `vector` 扩展、`products`、`image_assets`、`image_assets.vector` 的 `vector(1024)` 类型，以及关键表计数。结构证据输出为脱敏 JSON。

恢复前必须拒绝 unknown schema、failed/partial manifest、kind 或 purge batch 绑定不一致、哈希不一致的输入。

## 提交协议与数据流

1. 校验 backup ID、purge batch ID、专用配置和 PostgreSQL 16 client。
2. 获取源数据库脱敏身份与 server version。
3. 在 staging 写 custom dump，`fsync`，执行 `pg_restore --list`，计算大小和 SHA-256。
4. 原子发布本机 dump；写 attempt result，状态仍不是 complete。
5. 以不可覆盖方式上传 remote dump；下载到临时文件重算 SHA-256。
6. 生成并原子持久化不可变的 `candidate-manifest.json`，再不可覆盖上传远端并读回逐字节核对。同一 backup ID 重试必须复用 candidate 的全部字节和时间戳。
7. 将 candidate 原子重命名为本机 `manifest.json`；输出 `status=complete`。

本机与 OSS 无法组成跨系统事务。manifest-last 是提交标记：只有第 6、7 步完成的备份才可用于永久清除放行。任一步失败都返回 `status=failed`，保留可诊断 attempt result，并列出本机/远端可能存在的 partial/orphan 身份。后续用相同 backup ID 重试时执行 reconcile，不覆盖任何不一致对象。

## Manifest 契约

final manifest 至少包含：

- `schema_version`
- `status=complete`
- `backup_id`、`kind`、即时恢复点的 `purge_batch_id`
- UTC `created_at`、`completed_at`
- 脱敏数据库 identity、PostgreSQL client/server major version
- dump 文件名、格式、字节数、SHA-256
- 本机相对身份与 remote bucket/object identity
- `retention_days=30` 与 UTC `retain_until`
- 本机可读性、异机下载哈希和 manifest 读回验证证据
- production gates：私有桶、SSE、独立凭证、无 Delete 权限、30 天生命周期/Object Lock、真实恢复演练的验证状态

清单不得包含密码、原始 DSN、access key secret、签名 URL、图片内容或 embedding 向量。外部 production gate 未现场验证时必须明确为 `not_verified`，不能因写入 `retention_days=30` 就宣称策略已生效。

## 测试与证据

### 本轮必须运行的 fake 自动化

- fake runner 验证 argv 始终是列表、`shell=False`、环境显式传入、密码不出现在 argv/结果。
- 临时目录验证 0600 文件、原子发布、manifest-last、稳定 ID、批次绑定、重试 reconcile、冲突和各阶段失败。
- fake storage 验证不可覆盖、下载重算 SHA-256、final manifest 作为提交标记、无 Delete 接口。
- fake restore runner 验证只创建程序生成的新库、不使用 `--clean/--create`、不执行 drop、结构检查失败的稳定结果。
- CLI 测试验证四个子命令、退出码和 JSON 脱敏。

### 可选 disposable pgvector 集成测试

集成测试只有在显式配置相互独立的 disposable 源/恢复 pgvector PostgreSQL、专用源管理员与 acknowledgement 时运行，否则明确 skip。它在源实例创建随机数据库和固定样本 `products`、`image_assets`、`vector(1024)` 数据，执行真实 backup → remote fake storage → 从 remote 恢复到程序新建数据库 → 检查扩展、表、向量维度和固定计数。测试不自动删除源或恢复数据库。

本轮安全边界禁止连接真实 PostgreSQL，因此该测试只落盘，不执行。交付报告必须把 fake 自动化通过与真实恢复未验证分开。

## 运维与 production gate

恢复手册记录 PostgreSQL 16 client/独立 ops 环境前置条件、每日调度示例、恢复点调用、失败处置、异机恢复流程和证据保存。演练模板区分自动化证据与真实环境证据，并禁止记录凭证、签名 URL 或原始 DSN。

custom-format 全量备份提供一致快照，但不是 WAL/PITR；本方案 RPO 是最近一次每日备份或清除前即时恢复点。真实桶私有性、SSE、30 天生命周期/Object Lock、IAM 最小权限和一次从异机副本完成的真实隔离恢复演练全部验证前，永久清除 production gate 保持关闭。
