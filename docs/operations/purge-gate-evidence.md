# 永久清除安全门证据文档

本文件说明如何编写 `PURGE_GATE_EVIDENCE_DIR` 下的条件证明。它不是永久清除启用授权，也不替代 T14 现场验证。

应用进程只读这些 JSON，不执行备份、不加载 `.env.backup`、不访问 OSS。目录未配置或为空时五项均为 `unknown`，安全门关闭。

## 信任边界

能在应用可读位置写入本目录的主体，就能把五项打成 `valid`，从而使 `require_ready()` 通过。这是主机文件系统级控制，属于部署策略与 T14 现场验证，不是控制面模块能防御的威胁。

## 文件名

每个条件一个文件，只允许下列固定名：

- `daily_postgres_backup.json`
- `instant_restore_point_capability.json`
- `object_protection.json`
- `independent_backup_credentials.json`
- `recovery_drill.json`

## 合同

```json
{
  "schema_version": 1,
  "condition": "daily_postgres_backup",
  "result": "valid",
  "verified_at": "2026-08-22T04:00:00Z",
  "expires_at": "2026-08-23T05:00:00Z",
  "summary": "daily-2026-08-22 complete, copies verified"
}
```

- `result` 只允许 `valid` 或 `failed`。文档不得自称 `expired` 来绕过时钟。
- 过期由服务端根据 `expires_at` 与服务器时钟判定。
- 消费者还强制每项最大 age 与 `expires_at - verified_at`：`daily_postgres_backup` 26 小时、`instant_restore_point_capability` 60 分钟、`object_protection`/`independent_backup_credentials`/`recovery_drill` 24 小时。发布者写更长窗口只会得到 failed/expired，不能自行放宽。
- 文件不超过 64 KiB。
- 键名（含嵌套对象）不得出现 `password`、`secret`、`token`、`authorization`、`dsn`（大小写不敏感）。
- `summary` 会原样返回给已认证管理员。键名检查挡不住值泄漏，因此 summary 不得包含秘密、凭证、DSN、签名 URL 或私钥材料。

## #26 批次控制面

创建、取消、重试都经过 `require_ready()`。create 仅在安全门之前检查 `Idempotency-Key` 语法；门关闭时 HTTP 仍返回 `PURGE_GATE_NOT_READY`，审计可单独记录键语法原因。批次执行中若证据过期，取消会被拒绝，数据保留。GET 列表/详情只要求管理员认证，不要求安全门，以便观察卡住的批次。`pipeline_available()` 读取 worker 能力证明，TTL 120 秒，不能延长五项安全门窗口。若要让取消豁免安全门，必须作为边界变更另行授权。

正式删除 grant 与本目录和 pipeline heartbeat 完全独立：它最长 15 分钟，必须绑定 environment、deployment、单一 batch、排序 asset 集、数据库/对象 manifest、formal Bucket、对象调用上限及 no-overwrite trust attestation。当前生产 one-shot 入口不组合该文件读取器，不能因五项基础 gate ready 而执行删除。
