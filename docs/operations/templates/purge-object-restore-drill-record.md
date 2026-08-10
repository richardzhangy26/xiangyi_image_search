# 永久清除对象备份与隔离恢复演练记录

> 禁止填写密码、access key secret、签名 URL、原始 DSN、图片内容、embedding 向量或 SDK 原始错误响应。

## 基本信息

- 演练记录 ID：
- 操作者/复核人：
- 授权引用：
- 开始/结束 UTC 时间：
- purge batch ID：
- PostgreSQL backup ID：
- PostgreSQL final manifest Key / SHA-256：
- 对象 final manifest Key / SHA-256：
- 对象 manifest `authorization`：`backup_only_no_delete`
- 对象总数（源图/搜索预览）：
- `reference_protected` 搜索预览数：
- `retain_until`：

## 自动化和清单证据

- fake 单测命令与新鲜汇总：
- 静态无 Delete 合同结果：
- `verify-copies` 脱敏结果位置：
- plan 在 payload 前提交：`passed` / `failed`
- final manifest 最后提交：`passed` / `failed`
- 每项 source HEAD/download 大小与哈希：`passed` / `failed`
- 每项 backup HEAD/download 大小与 SHA-256：`passed` / `failed`
- 数据库/对象 `purge_batch_id` 绑定：`passed` / `failed`

自动化 fake 结果不能勾选下方真实环境 production gate。

## 真实引用完整性证据

- [ ] 同一只读、可重复读快照覆盖选中 archived assets
- [ ] `image_assets` active/archived 引用切片完整
- [ ] 未结束 `image_import_items` 引用切片完整
- [ ] 切片 consistency token 一致、未截断、计数匹配
- [ ] 删除前在锁/fence 内重新验证引用图和正式对象身份

证据引用（脱敏查询记录、快照标识或审计工单）：

## 真实 OSS 与 IAM 证据

- [ ] 正式、备份、隔离 Bucket 三者不同
- [ ] 正式读取角色仅有 Head/Get
- [ ] 备份 Bucket ACL 为 private
- [ ] 备份对象 SSE 或 Bucket 默认加密已验证
- [ ] 备份角色仅允许指定前缀 Put/Head/Get，明确无 Delete
- [ ] 日常应用凭证不能访问备份 Bucket
- [ ] 隔离恢复角色不能写正式或备份 Bucket
- [ ] 30 天生命周期、Object Lock 或等效不可提前删除策略已验证

策略/审计证据引用（不粘贴凭证）：

## 真实隔离恢复证据

- `PURGE_RESTORE_ISOLATED=1` 的授权引用：
- restore run ID：
- 隔离 Bucket / 程序派生前缀：
- 恢复对象数与总字节数：
- 备份下载重算 SHA-256：`passed` / `failed`
- 隔离写后 HEAD：`passed` / `failed`
- 隔离下载重算 SHA-256：`passed` / `failed`
- 未写回任一正式 Key：`confirmed` / `not_confirmed`
- 未调用任何 Delete：`confirmed` / `not_confirmed`
- 恢复结果：`verified` / `failed`
- 失败 stage/error code（如有）：
- 隔离对象后续人工处置记录：

## Production gate 结论

- [ ] 所有证据均为本次新鲜证据
- [ ] 所有 Bucket 与 IAM 身份已现场核对
- [ ] 真实隔离恢复已完成并由第二人复核
- [ ] 未发现凭证、签名 URL、图片内容或向量泄露
- [ ] 本记录只作为后续 gate 评估材料，不自行授权正式删除

最终结论：`not_verified` / `verified`

未满足项与后续动作：
