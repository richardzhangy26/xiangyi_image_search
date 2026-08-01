# Kodo → 私有 OSS 图片资产迁移

正式迁移入口是 `scripts.migrate_kodo_to_oss.py`。旧
`migrate_oss_path.py` 只会拒绝执行，不再改写 `product_images.oss_path`
或拼接公开七牛 URL。

旧 `scripts.ingest_images.py` 也不再写本机 `uploads` 或
`product_images`；它只保留 `--dry-run` 只读盘点。不要用旧参数
`--rebuild-index` 尝试导入，写模式会在扫描和 embedding 之前明确拒绝。
本地文件夹写入 `ImageAsset` 的新入口尚未设计，不能回退到旧表。

## 安全顺序

在 `backend` 目录运行：

```bash
# 轻量只读连通性检查：列举来源并 HEAD/GET/解码一张小图
python -m scripts.migrate_kodo_to_oss --preflight

# 默认也是 dry-run；仅扫描、筛选和报告，不构造 OSS/embedding 写端
python -m scripts.migrate_kodo_to_oss --dry-run \
  --report-path reports/kodo-dry-run.json

# 显式试迁移，不会自动继续全量
python -m scripts.migrate_kodo_to_oss --pilot 10 \
  --batch-size 20 \
  --report-path reports/kodo-pilot.json

# 只重试上一份报告中 status=failed 的项；冲突不会自动重试
python -m scripts.migrate_kodo_to_oss --pilot 10 \
  --retry-failed reports/kodo-pilot.json \
  --report-path reports/kodo-pilot-retry.json

# 仅在试迁移和人工验收通过后显式运行
python -m scripts.migrate_kodo_to_oss --full \
  --report-path reports/kodo-full.json
```

不提供 `--pilot` 或 `--full` 时始终是只读 dry-run。`--pilot`、`--full`、
`--dry-run` 和 `--preflight` 互斥；`--batch-size` 自动限制到 1–20。

进入 `--pilot` 或 `--full` 后，命令会在构造 embedding 和下载来源图片
之前只读核对 OSS 目标：实际 Bucket 名、Endpoint 对应地域、Bucket ACL
必须为 `private`，`OSS_IMAGE_BASE_PREFIX` 必须是非根级隔离前缀。若前缀
中已有对象，还会抽查其迁移元数据；任何一项不匹配都会中止写模式。

## 筛选参数

- `--prefix PREFIX`：只扫描指定 Kodo 前缀。
- `--limit N`：进一步限制本次选择的图片数。
- `--retry-failed REPORT.json`：只选择旧报告中 `status=failed` 的来源
  Key。单独使用时仍是 dry-run；要写入必须同时显式指定 `--pilot N` 或
  `--full`。旧报告必须带有来源 provider、Bucket、S3 Bucket 和 prefix
  绑定；当前运行与这些字段不一致时会在列举对象前拒绝重试。
- `--report-path PATH`：原子写入完整 UTF-8 JSON 报告。

## 报告

报告包括：

- 扫描对象数、图片数、非图片数和字节数；
- 实际选择的图片数和字节数；
- 下载、原图、预览、embedding、数据库各阶段的新增、复用、冲突和失败；
- 每个来源 Key 的最终状态、失败阶段和脱敏错误码；
- retry 请求数、匹配数及来源中已不存在的 Key；
- 总耗时。

终端只输出汇总和最多 5 个失败或冲突示例，不输出逐项 `items`。完整
逐项明细仅在指定 `--report-path` 时原子写入该文件；需要审计或后续
`--retry-failed` 时应始终指定报告路径。有失败或冲突时命令返回非零
退出码。报告不会保存凭证、签名 URL、原始下游异常文本或临时文件路径。

## 不变量

- Kodo 全程只读，不执行 Put/Delete。
- OSS 原图和预览禁止覆盖；已存在对象必须通过 HEAD 校验。
- 同来源路径同内容重跑保持幂等。
- 同来源路径内容变化只报告 `source_conflict`。
- 不同来源路径内容相同仍建立独立图片资产，并复用兼容预览和向量。
- 本仓库只交付迁移能力和自动化测试；不会自动执行真实试迁移或全量迁移。
