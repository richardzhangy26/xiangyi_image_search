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

# 按精确清单只读下载、哈希和验证 10 张试迁移样本；不会构造写端
python -m scripts.migrate_kodo_to_oss --verify-selection \
  --selection-manifest reports/issue-10/selection.json \
  --report-path reports/issue-10/selection-verification.json

# 显式试迁移，不会自动继续全量
python -m scripts.migrate_kodo_to_oss --pilot 10 \
  --selection-manifest reports/issue-10/selection.json \
  --verified-selection-report reports/issue-10/selection-verification.json \
  --batch-size 20 \
  --report-path reports/kodo-pilot.json

# 仅在 #11 已获授权后，重试上一份全量报告中 status=failed 的项；冲突不会自动重试
python -m scripts.migrate_kodo_to_oss --full \
  --full-authorization reports/issue-11/full-authorization.json \
  --retry-failed reports/kodo-full.json \
  --report-path reports/kodo-full-retry.json

# 仅在试迁移、Issue 证据和人工批准齐备后显式运行
python -m scripts.migrate_kodo_to_oss --full \
  --full-authorization reports/issue-11/full-authorization.json \
  --report-path reports/kodo-full.json
```

不提供 `--pilot` 或 `--full` 时始终是只读 dry-run。`--pilot`、`--full`、
`--dry-run`、`--preflight` 和 `--verify-selection` 互斥；`--batch-size` 自动限制到 1–20。

进入 `--pilot` 或 `--full` 后，命令会在构造 embedding 和下载来源图片
之前只读核对 OSS 目标：实际 Bucket 名、Endpoint 对应地域、Bucket ACL
必须为 `private`，`OSS_IMAGE_BASE_PREFIX` 必须是非根级隔离前缀。若前缀
中已有对象，还会抽查其迁移元数据；任何一项不匹配都会中止写模式。

## 筛选参数

- `--prefix PREFIX`：只扫描指定 Kodo 前缀。
- `--limit N`：进一步限制本次选择的图片数。
- `--selection-manifest PATH`：UTF-8 JSON 文件，且只包含有序、唯一的
  `source_relative_paths` 数组。它只能用于 `--dry-run`、
  `--verify-selection` 或 `--pilot`；不能与 `--retry-failed` 并用，
  `--pilot` 只能是 10，且清单必须恰好 10 项；`--full` 一律拒绝该参数。
- `--verified-selection-report PATH`：`--pilot` 的必需输入，必须是同一清单
  成功运行 `--verify-selection` 生成的完整报告。写入前会重新下载每张 Kodo
  源图并比对路径、来源绑定、覆盖结论和 SHA-256；任一不一致都不会构造 OSS、
  embedding 或数据库写端。通过后，入库服务只读取该次验证生成的临时快照，
  不会再次读取 Kodo，避免验证与写入之间的内容变化。
- `--retry-failed REPORT.json`：只选择旧报告中 `status=failed` 的来源
  Key。单独使用时仍是 dry-run；要写入必须同时显式指定 `--pilot N` 或
  `--full`。旧报告必须带有来源 provider、Bucket、S3 Bucket 和 prefix
  绑定；当前运行与这些字段不一致时会在列举对象前拒绝重试。
- `--report-path PATH`：原子写入完整 UTF-8 JSON 报告。
- `--full-authorization PATH`：`--full` 的必需输入。它是本地受控 JSON，
  绑定 #9、#10 证据、用户批准、数据库恢复点以及刚完成的 preflight/dry-run
  报告的 SHA-256；路径和批准 URL 不写入迁移报告或数据库。

## Issue #10 的精确选样

先完成 `--preflight` 和完整 `--dry-run`，再从实时盘点报告固定十个真实
Kodo Key。清单文件只放在本地受控证据目录，不提交到仓库：

```json
{
  "source_relative_paths": [
    "来自 inventory.json 并已验证通过的精确 Kodo Key"
  ]
}
```

示例中的文字不是可用的来源路径，必须替换为本次盘点发现的十项真实 Key。
写入前运行 `--verify-selection`；它仅执行 Kodo 的 list/HEAD/GET，逐项
检查 SHA-256、真实格式和尺寸，并报告覆盖标签。通过的清单必须同时覆盖：

- 含中文和空格的路径、多层目录；
- 实际 JPEG、PNG、WebP；
- 超过 20 MiB 的源图与一张不应放大的小图；
- 一组不同路径但 SHA-256 相同的图片。

任何缺项或解码失败均返回非零，且不会开始 OSS、embedding 或数据库写入。
验证报告不包含凭证、完整签名 URL 或临时绝对路径。

如果试迁移本身出现失败或冲突，停止在 #10：修正来源或清单后，重新运行
`--verify-selection`，再用同一份清单和新验证报告重新运行 `--pilot 10`。
此时不得使用 `--full` 或 `--retry-failed` 绕过 #10 验收。

## Issue #11 的受控授权文件

只有 #10 的报告、对账和搜索截图已经附到 Issue #10，且用户在 Issue #10 或
父 PRD 留下明确全量批准后，才创建本地授权文件。文件路径保持在受控证据目录，
不提交仓库：

```json
{
  "issue_9_url": "https://github.com/richardzhangy26/xiangyi_image_search/issues/9",
  "issue_10_evidence_url": "https://github.com/richardzhangy26/xiangyi_image_search/issues/10#issuecomment-...",
  "user_approval_url": "https://github.com/richardzhangy26/xiangyi_image_search/issues/10#issuecomment-...",
  "database_backup_reference": "pg_dump image_search 2026-08-02T10:00+08:00",
  "preflight_report": {"path": "../issue-10/preflight.json", "sha256": "<64 位小写 SHA-256>"},
  "dry_run_report": {"path": "../issue-10/dry-run.json", "sha256": "<64 位小写 SHA-256>"}
}
```

两个报告必须是成功、只读且来源绑定一致的本次 `preflight` 和完整 `dry-run`
结果；它们均包含生成时间，必须在过去 24 小时内按顺序生成，且对象数、图片数、
非图片数和总字节数必须相等。授权文件会再次核验报告哈希、统计和当前来源
Bucket、S3 Bucket、prefix，并要求本次 `--full` 枚举的完整统计与基线完全相同。

`--full` 还通过 `gh api` 实时验证：Issue #9 和 #10 都已关闭、
`issue_10_evidence_url` 对应的 API 评论确属 Issue #10 且明确包含
preflight/dry-run/pilot 验收材料、批准引用是本仓库 Issue #10 中由
`richardzhangy26` 发布的明确正向“批准全量迁移”评论，或是同一仓库父 PRD 的
GitHub 文件链接且其正文含明确正向批准。否决或含糊表述不会通过。缺少任一项、
API 不可用、报告不匹配或未记录恢复点时，`--full` 都以非零状态拒绝执行。

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
- 本仓库不会自动执行真实试迁移或全量迁移；真实操作须由操作者显式运行受控命令。
- #10 试迁移完成后必须附上脱敏报告、对象/数据库对账和搜索截图，并停止；
  只有用户在 #10 或父 PRD 留下明确批准、再次完成 preflight/dry-run 并保存
  数据库恢复点后，才允许手工运行 `--full`。
