# 永久清除对象备份清单与隔离恢复手册

## 适用范围

本手册对应 Issue #25，只处理永久清除前的正式图片对象备份证据、对象副本复验和仅隔离位置恢复。它不创建永久清除批次，不授权或执行正式 OSS Delete，也不删除数据库记录、向量、备份对象或隔离对象。

对象备份创建没有独立 CLI。后续 Issue #26 只能在已认证、已持久化的清除批次状态机中调用 `PurgeObjectBackupService.create_verified()`；本 Ticket 的 CLI 只有 `verify-copies` 和 `restore-isolated`，不能绕过批次与数据库恢复点门控。

## 放行前必须成立的合同

1. PostgreSQL 恢复点是 Issue #24 的 `status=complete`、`kind=purge_restore_point` final manifest。
2. 数据库恢复点、对象 plan、payload 和对象 final manifest 使用同一个 `purge_batch_id`，backup ID 固定为 `purge-<batch-id>`。
3. 引用快照目录 v1 同时覆盖：
   - `image_assets` 的 `active` 与 `archived` 引用；
   - `image_import_items` 的全部未结束引用，即使为零也必须提供完整空切片。
4. 每个目标资产必须为 `archived`。源图必须是该资产的唯一引用；搜索预览只有在本批移除后没有其他引用时才进入备份清单。
5. 共享且仍有引用的搜索预览记录为 `reference_protected`，不复制，也不成为删除候选。
6. `plan.json` 在任何 payload 前以不可覆盖方式提交；所有 payload 通过写后 HEAD 和独立下载 SHA-256 校验后，`manifest.json` 才作为最后提交标记。
7. final manifest 的 `authorization` 只能是 `backup_only_no_delete`。它是备份证据，不是删除授权。

当前仓库没有 PostgreSQL 引用快照生产 Adapter，也没有对象备份创建 CLI，因此生产对象备份 gate 仍然关闭。

## 对象与凭证边界

所有角色只注入独立 ops 进程；`backend/.env.backup` 不得注入 Flask、Gunicorn、frontend、compose 健康检查或日常应用容器。

| 角色 | 配置 | 允许能力 | 禁止能力 |
|---|---|---|---|
| 正式对象只读 | `PURGE_SOURCE_OSS_*` | Head/Get | Put/Delete/List/签名/ACL |
| 独立备份 | `BACKUP_OSS_*` | 指定前缀 Put-if-absent/Head/Get | Delete/覆盖/ACL/生命周期管理 |
| 隔离恢复 | `PURGE_RESTORE_OSS_*` | 隔离前缀 Put-if-absent/Head/Get | 正式 Key 写入/Delete/覆盖/签名 |
| 日常应用 | `OSS_*` | 既有应用图片工作流 | 访问备份 Bucket 或隔离 Bucket |

正式、备份、隔离三个 Bucket 必须互异；四类 access-key identity 必须互异。代码中的窄 Interface 不能证明真实 IAM 权限，真实策略必须另行审计。

## 复制和校验语义

- 正式对象先 HEAD，再下载到 0600 临时文件并按实际字节计算大小与 SHA-256。
- 源图实际大小/哈希同时核对 `image_assets.source_size/content_hash`。
- 搜索预览的 SHA-256 必须从预览本体计算；既有 preview metadata 中的 `sha256` 是源图哈希，不能替代预览哈希。
- 备份对象 ID 为正式 Bucket 与 Key 的稳定摘要；备份 Key 不嵌入原始路径。
- 已存在目标只有在 metadata、大小以及独立下载后的 SHA-256 全部一致时才可 reconcile；任一不一致即冲突，绝不覆盖。
- 同一批次中断后只允许使用同一个不可变 plan 重试。partial/orphan 不自动删除。
- 清单继承数据库恢复点的精确 30 天 `retain_until`，所有真实环境 production gate 保持 `not_verified`。

## 离线 fake 验收

以下命令不会连接 PostgreSQL 或 OSS：

```bash
cd backend
python -m pytest \
  test/test_purge_object_backup.py \
  test/test_purge_object_restore.py \
  test/test_purge_object_storage.py \
  test/test_manage_purge_object_backups.py \
  test/test_purge_object_backup_contract.py -v
```

这些测试只能证明引用算法、不可覆盖协议、读回校验、隔离目标派生和静态无 Delete 接线；不能关闭任何真实环境 gate。

## 复验既有对象副本

以下命令仅适用于未来已由受控批次状态机产生的 complete 对象 manifest。本 Ticket 不执行真实命令：

```bash
cd backend
umask 077
python scripts/manage_purge_object_backups.py verify-copies \
  --manifest backups/purge-example-001/objects/manifest.json
```

成功退出码为 `0`，stdout JSON 的 `status` 为 `verified`。工具会从固定批次前缀重新下载远端 final manifest 和每个 payload，并核对 exact-schema、canonical JSON、Bucket/Key、metadata、大小与 SHA-256。

## 仅恢复到隔离位置

真实恢复前必须另行获得授权，确认目标 Bucket 是一次性隔离环境，再临时把 `PURGE_RESTORE_ISOLATED` 从默认 `0` 改为 `1`：

```bash
cd backend
umask 077
python scripts/manage_purge_object_backups.py restore-isolated \
  --manifest backups/purge-example-001/objects/manifest.json \
  --restore-run-id drill-example-001 \
  --acknowledge-isolated
```

目标 Key 由程序固定派生为：

```text
<isolated-prefix>/<restore-run-id>/purge-<batch-id>/objects/<kind>/<object-id>
```

调用方不能提交目标 Key。工具先复验远端 final 与全部备份对象，再重新下载 payload、不可覆盖写入隔离 Bucket、重新 HEAD 并独立下载计算 SHA-256。它不会写回正式 Key，不生成签名 URL，也不删除任何对象。

## 固定退出码与处置

| 退出码 | 含义 | 处置 |
|---|---|---|
| 0 | 副本或隔离恢复已验证 | 保存脱敏 stdout JSON 和清单摘要 |
| 2 | 参数、配置或隔离门错误 | 停止；不得回退应用凭证或放宽 Bucket 身份 |
| 3 | manifest/引用/完整性错误 | 停止永久清除；核对同批次证据 |
| 4 | 备份对象存储错误 | 保持同一批次和 Key 重试，不覆盖 |
| 5 | 隔离恢复校验或冲突 | 保留证据；不得改写正式位置 |

错误输出经过脱敏，不应保存 SDK 原始响应。禁止记录凭证、签名 URL、原始 DSN、图片内容或 embedding 向量。

## 仍未验证的 production gate

- 真实 PostgreSQL `READ ONLY REPEATABLE READ` 引用快照覆盖所有来源；
- 正式读取 IAM 确为 Head/Get-only；
- 备份 Bucket private、SSE、写凭证无 Delete；
- 30 天生命周期、Object Lock 或等效不可提前删除策略；
- 应用凭证不能访问备份 Bucket；
- 隔离 Bucket 与正式/备份环境真实独立；
- 一次从真实备份 Bucket 到真实隔离 Bucket 的恢复演练。

使用 [对象恢复演练记录模板](templates/purge-object-restore-drill-record.md) 保存未来的受控证据。

## TOCTOU 安全边界

对象 manifest 只证明创建时的备份和引用关系。引用可能在 manifest 完成后变化；后续删除开始前必须在写入 fence、锁或等效串行化边界内重新抓取完整引用图，并重新核对正式对象版本/实际字节身份。`revalidate_current_candidates()` 只能缩减已备份候选，不能把先前未备份的对象补入。任一变化都不能用本清单直接授权删除。
