# PostgreSQL 恢复演练记录

> 本模板禁止填写密码、原始 DSN、access key secret、签名 URL、图片内容或完整 embedding 向量。

## 基本信息

- 演练记录 ID：
- 操作者/复核人：
- 授权引用：
- 开始/结束 UTC 时间：
- backup ID：
- backup kind：`daily` / `purge_restore_point`
- purge batch ID（仅恢复点）：
- final manifest SHA-256：
- 脱敏源数据库 identity：
- PostgreSQL client/server major：

## 自动化证据

- fake runner/fake storage 定向测试命令：
- 测试原始汇总（passed/failed/skipped）：
- `verify-copies` 结果文件位置：
- 本机 SHA-256：
- 异机下载重算 SHA-256：

自动化证据不能勾选下方真实环境 production gate。

## 真实备份基础设施证据

- [ ] 备份 Bucket 与正式图片 Bucket 不同
- [ ] Bucket ACL 为 private
- [ ] Bucket 默认加密或对象 SSE 已现场验证
- [ ] 备份 IAM 仅允许专用前缀 Put/Head/Get
- [ ] 备份 IAM 无 Delete/ACL/生命周期管理权限
- [ ] 应用运行凭证不能访问备份 Bucket
- [ ] 30 天生命周期、Object Lock 或等效策略已验证

证据引用（策略 ID、只读截图或受控审计记录，不粘贴凭证）：

## 真实隔离恢复证据

- 显式 disposable 授权引用：
- 隔离实例脱敏 identity：
- 程序生成的目标数据库名：
- 从异机副本下载时间与对象身份：
- `vector` extension：
- `products` 表与行数：
- `image_assets` 表与行数：
- `image_assets.vector` 类型：
- 恢复结果：`verified` / `failed`
- 失败 stage/error code（如有）：
- 目标数据库后续人工处置记录：

## Production gate 结论

- [ ] 所有真实基础设施证据均为本次新鲜证据
- [ ] 已完成一次从异机副本到独立 pgvector 实例的真实恢复
- [ ] 未发现凭证、DSN 或签名 URL 泄露
- [ ] 复核人确认永久清除 gate 可另行评估

最终结论：`not_verified` / `verified`

未满足项与后续动作：
