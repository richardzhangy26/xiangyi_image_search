# PostgreSQL 备份、异机副本与隔离恢复手册

## 适用范围

本手册对应 Issue #24，只处理 PostgreSQL 16 + pgvector 的每日全量备份、永久清除批次即时恢复点、双副本校验和隔离恢复验证。它不是 WAL/PITR，也不启用永久清除。

CLI 只在显式调用时运行；应用启动和健康检查不会执行备份。所有命令必须在独立 ops 进程中运行，禁止把 `.env.backup` 注入 Flask/Gunicorn。

## 未满足前必须保持关闭的 production gate

以下项目默认均为 `not_verified`，不能由 fake 自动化替代：

1. 备份 Bucket 与正式图片 Bucket 独立，ACL 为 private。
2. Bucket 默认加密或对象 SSE 已现场验证。
3. 备份凭证只允许专用前缀的 Put/Head/Get，无 Delete、ACL 和生命周期管理权限；应用运行凭证不能访问备份 Bucket。
4. 30 天生命周期、Object Lock 或等效不可提前删除策略已现场验证。
5. 已从异机副本在独立 disposable pgvector PostgreSQL 完成一次真实恢复演练。

任一项没有新鲜证据时，不得开放永久清除。

## 运行前置条件

- 独立 ops 主机或镜像提供 `pg_dump`、`pg_restore`、`psql`、`createdb`，四者 major version 都是 PostgreSQL 16。
- 源 PostgreSQL server major version 是 16。
- 复制 [backend/.env.backup.example](../../backend/.env.backup.example) 为未跟踪的 `backend/.env.backup`，通过 secret manager 填充独立凭证。
- 本机备份目录位于受保护磁盘，操作者以 `umask 077` 运行。
- 真实桶创建、IAM、生命周期、生产备份和恢复演练已另行取得明确授权。

只读检查：

```bash
cd backend
command -v pg_dump pg_restore psql createdb
pg_dump --version
pg_restore --version
psql --version
createdb --version
```

任一命令缺失或不是 major 16，停止。不要把密码或连接串放进命令行参数。

## 创建每日全量备份

由外部 cron/launchd/调度器每天显式调用一次；不要修改 Flask 或 compose 健康检查：

```bash
cd backend
umask 077
python scripts/manage_postgres_backups.py create-daily
```

成功退出码为 `0`，唯一 stdout JSON 的 `status` 必须为 `complete`。每日标识使用 `BACKUP_DAILY_TIMEZONE` 的 `daily-YYYY-MM-DD`，同日重试只允许 reconcile 相同工件。

## 创建清除批次即时恢复点

清除 worker 后续只能在获得 `status=complete` 后放行；本任务不接入 worker：

```bash
cd backend
umask 077
python scripts/manage_postgres_backups.py create-restore-point \
  --purge-batch-id purge-batch-example-001
```

恢复点 ID 稳定为 `purge-<purge_batch_id>`。同一批次重试复用相同本机目录和远端对象键；内容、数据库身份或批次绑定不一致时必须停止，不能改用新 ID 绕过冲突。

## 重新校验本机与异机副本

```bash
cd backend
python scripts/manage_postgres_backups.py verify-copies \
  --manifest backups/purge-purge-batch-example-001/manifest.json
```

成功要求本机 SHA-256 与异机下载后重算 SHA-256 都等于 final manifest。仅有 `attempt-result.json`、`status=failed/partial` 或未知 schema 的记录不是有效备份。

## 在隔离 PostgreSQL 验证恢复

先由授权人员确认 `RESTORE_VERIFY_DB_*` 指向独立 disposable pgvector 实例，再临时设置 `RESTORE_VERIFY_DISPOSABLE=1`。目标数据库名由程序生成；工具拒绝已有目标，不使用 `--clean`/`--create`，也不自动执行 drop。

```bash
cd backend
umask 077
python scripts/manage_postgres_backups.py verify-restore \
  --manifest backups/purge-purge-batch-example-001/manifest.json \
  --acknowledge-isolated
```

恢复始终从异机副本下载，不复用本机 dump。成功证据至少包含：

- `vector` extension 存在；
- `products` 与 `image_assets` 存在；
- `image_assets.vector` 是 `vector(1024)`；
- 两个关键表的行数。

无论成功或失败，程序都保留新建的 `backup_verify_<随机值>` 数据库，不自动删除。记录证据后，由获授权人员确认目标仅属于 disposable 环境，再按当地变更流程清理。

仓库的可选集成测试还要求独立 `DISPOSABLE_SOURCE_ADMIN_DB_*` 配置。只有两个显式门同时为 `1` 时，它才会先只读确认源/恢复地址与 PostgreSQL system identity 均不同，再在非生产源实例创建随机 `backup_source_<随机值>` 数据库、写入固定的 2 条产品与 3 条图片向量，并授予专用备份角色只读权限后执行备份与恢复。源与恢复数据库都不会自动删除；由 disposable 环境生命周期负责清理。测试的远端副本是进程内内存伪存储，不会访问 OSS。

## 失败和 partial/orphan 处置

固定退出码：

| 退出码 | 含义 | 处置 |
|---|---|---|
| 0 | complete/verified | 保存 stdout JSON 与 final manifest |
| 2 | 配置或安全前置错误 | 修正独立配置，不要回退应用凭证 |
| 3 | dump、manifest 或完整性错误 | 停止清除；核对 `attempt-result.json` |
| 4 | 异机存储错误 | 停止清除；保持相同 backup ID 重试 reconcile |
| 5 | 恢复或隔离安全门错误 | 不得改指生产库；保存目标数据库名和证据 |

本机与 OSS 不能组成事务。提交顺序是：本机 dump 原子发布 → remote dump 下载重算哈希 → remote final manifest 写入并读回 → 本机 final manifest 原子写入。final manifest 不存在时，partial/orphan 不能供永久清除放行。

不要自动删除 partial/orphan。先用相同 backup ID 重试；若出现冲突，保存本机路径、远端对象键和脱敏错误，升级人工处理。任何覆盖或删除都需要单独授权。

## 证据保存与禁止内容

使用 [恢复演练记录模板](templates/postgresql-restore-drill-record.md) 保存命令时间、backup ID、manifest SHA-256、脱敏数据库身份、结构检查和 production gate 证据。

禁止记录：数据库密码、原始 DSN、access key secret、签名 URL、图片内容、完整 embedding 向量或外部工具含敏感信息的原始 stderr。

## RPO 边界

custom-format `pg_dump` 提供单次一致快照，不提供时间点恢复。可恢复点是最近一次 `complete` 每日备份，或对应清除批次的 `complete` 即时恢复点；两次备份之间的写入不在本方案 RPO 内。
