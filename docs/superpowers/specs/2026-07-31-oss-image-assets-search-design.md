# OSS 图片资产迁移与图片级搜索设计

- 日期：2026-07-31
- 状态：已自审，待用户评审
- 本期范围：七牛 Kodo → 阿里云 OSS、独立图片资产、搜索预览图、图片级 pgvector 检索、大查询图处理
- 延期范围：Excel、商品资料补录、图库文件夹拖拽上传
- 相关领域文档：[CONTEXT.md](../../../CONTEXT.md)
- 相关决定：[ADR-0001](../../adr/0001-image-assets-independent-from-products.md)、[ADR-0002](../../adr/0002-rank-search-results-by-image-asset.md)、[ADR-0003](../../adr/0003-oss-as-authoritative-image-store.md)、[ADR-0004](../../adr/0004-progressive-product-enrichment.md)

## 1. 目标

将七牛 Kodo 存储桶 `xiangxipackage` 中的图片迁移到私有阿里云 OSS，并在没有商品型号和完整商品资料的情况下，为每张来源路径建立独立、可搜索的图片资产。

完成后，用户上传查询图片即可得到图片级 Top-K 结果。每条结果至少显示：

- 搜索预览图
- 相似度
- 完整来源相对路径
- 可空的商品型号

## 2. 已核对的现场

### 2.1 云端与数据

- 七牛 Kodo：`xiangxipackage`
- 对象总数：2420
- 图片对象：2419
- 图片总量：约 5.967 GiB
- 最大图片：约 56.17 MiB
- 超过 20 MiB：24 张
- 存在少量“内容完全相同、来源路径不同”的图片
- 阿里云 OSS Bucket：私有，上海地域
- OSS 中已经存在其他业务前缀，本项目必须使用隔离前缀

以上数字是设计阶段快照。实际迁移前必须重新扫描，不把快照当成最终事实。

### 2.2 当前代码

- `ProductImage` 必须关联 `Product.model_number`
- `products` 的型号、摄影师文件、阿里链接、分类均为必填
- `product_images.content_hash` 全库唯一
- 搜索 SQL 用 `DISTINCT ON (model_number)` 按型号折叠
- 图片默认落在本地 `backend/uploads/product_images/`
- 后端会把 embedding 输入转为 JPEG 并尝试压到 2.5 MiB，但没有最长边约束、EXIF 方向处理和最终大小保证
- 前端直接上传原查询图，不做预压缩
- Flask 请求限制 16 MiB，Nginx 限制 20 MiB
- 查询临时文件会在请求结束后删除
- OSS 与七牛 SDK 依赖已经存在
- `backend/.env` 已被 Git 与 Docker 构建上下文忽略
- Docker Compose 已传递 OSS 变量，但本机只写在 `backend/.env` 时，仍需统一容器加载方式
- 旧 `backend/scripts/migrate_oss_path.py` 名为 OSS 迁移，实际生成七牛公开 URL，不能用于本次迁移

## 3. 已确认的产品与领域规则

1. 每个来源相对路径是一项独立图片资产，文件夹不代表型号。
2. 图片资产可以没有商品型号，仍可生成向量和参与搜索。
3. 多项图片资产可以关联同一商品型号，关联不合并资产。
4. 搜索结果按图片资产逐条排序，不按商品型号折叠。
5. 不同路径内容相同，也保留为不同资产和不同搜索结果。
6. 相同内容可以复用搜索预览图与 embedding，避免重复处理和计费。
7. 首批迁移不从目录或文件名推断型号、分类或其他商品资料。
8. OSS 是迁移完成后的正式图片源；Kodo 只作迁移期只读备份，不双写、不自动删除。
9. 源图原样保留；搜索预览图是额外的持久化派生文件。
10. 查询图只服务于单次搜索，不进入 OSS 或数据库。
11. 同一来源相对路径内容改变时，不自动覆盖，报告为来源冲突。
12. 日常删除采用归档；永久清除需要独立、明确的操作。
13. 商品资料允许以后渐进补全，但补录 UI 和 Excel 本期延期。

## 4. 本期边界

### 4.1 本期交付

- 独立 `image_assets` 数据结构
- Kodo → OSS 迁移 CLI
- 原图与搜索预览图双层存储
- 私有 OSS 图片访问
- 图片级向量检索
- 无型号搜索结果与来源相对路径展示
- 大查询图传输预处理与后端标准化
- 10 张试迁移、验收后全量迁移
- 自动化测试、迁移报告和可重跑能力

### 4.2 本期不做

- Excel 导出、导入和字段清空语义
- 手工补型号或完整商品资料的页面
- 图库文件夹拖拽批量上传
- 根据文件夹、文件名自动推断型号或分类
- Kodo 删除
- OSS 原图永久清除入口
- 文搜图、图文融合搜索

延期项目不得通过占位按钮或未完成 API 混入本期。

## 5. 目标架构

```text
七牛 Kodo
  │ 只读列举/下载
  ▼
迁移编排器
  ├─ 计算 SHA-256、校验图片
  ├─ 上传原图到私有 OSS
  ├─ 生成并上传搜索预览图
  ├─ 批量生成 embedding
  └─ 写入 PostgreSQL image_assets

查询图片
  │ 浏览器传输预处理
  ▼
后端统一图片标准化
  ▼
DashScope 1024 维 embedding
  ▼
pgvector 图片级 Top-K
  ▼
相对路径 + 私有 OSS 预览 + 相似度
```

### 5.1 模块职责

| 模块 | 职责 |
|---|---|
| `ImageNormalizer` | EXIF 方向、色彩/透明背景、尺寸与大小约束、稳定 JPEG 输出 |
| `ObjectStorage` | OSS 上传、HEAD 校验、私有签名、对象元数据 |
| `ImageAssetIngestService` | 建立图片资产、重复内容复用、事务与失败恢复 |
| `VectorSearchService` | 图片级 pgvector 检索，不了解商品折叠 |
| Kodo 迁移 CLI | 列举 Kodo、下载临时文件、调用入库服务、生成报告 |
| 图片资产 API | 搜索结果、预览重定向、归档状态查询 |

服务之间通过清晰的数据对象传递，不让蓝图直接实现 OSS、图片压缩或迁移逻辑。

## 6. 数据模型

### 6.1 `image_assets`

推荐字段：

| 字段 | 类型 | 约束/含义 |
|---|---|---|
| `id` | UUID | 主键；对外称 `asset_id` |
| `model_number` | VARCHAR(100) | 可空，FK → `products.model_number`，删除商品时 `SET NULL` |
| `source_provider` | VARCHAR(32) | 首批固定为 `qiniu-kodo` |
| `source_bucket` | VARCHAR(255) | 首批为 `xiangxipackage` |
| `source_relative_path` | TEXT | 原始 Kodo Object Key，UTF-8 原样保存 |
| `source_revision` | INTEGER | 同一来源路径的内容修订号，默认 1 |
| `oss_path` | TEXT | OSS 原图 Object Key，唯一 |
| `preview_oss_path` | TEXT | 搜索预览图 Object Key，可被同哈希资产共享 |
| `content_hash` | VARCHAR(64) | 源图 SHA-256，不唯一 |
| `source_size` | BIGINT | 原图字节数 |
| `source_mime_type` | VARCHAR(100) | 校验后的媒体类型 |
| `source_width` / `source_height` | INTEGER | 原图尺寸 |
| `vector` | vector(1024) | 非空 |
| `embedding_model` | VARCHAR(128) | 生成该向量的模型名 |
| `embedding_dimension` | SMALLINT | 当前为 1024 |
| `normalization_version` | VARCHAR(32) | 当前为 `preview-v1` |
| `status` | VARCHAR(20) | `active` 或 `archived` |
| `archived_at` | TIMESTAMP | 可空 |
| `created_at` / `updated_at` | TIMESTAMP | 系统时间 |

约束与索引：

- UNIQUE `(source_provider, source_bucket, source_relative_path, source_revision)`
- UNIQUE `(oss_path)`
- 普通索引 `(content_hash)`，允许多条记录同哈希
- 普通索引 `(model_number)`
- 普通索引 `(status)`
- 针对 `status = 'active'` 的部分 HNSW `vector_cosine_ops` 索引

`source_relative_path` 是用户可读定位信息，不承担数据库实体身份；`id` 才是不可变身份。首批迁移只写 revision 1；来源冲突只报告，不在本期自动创建新修订。

新表不把临时下载位置作为正式字段。若兼容响应仍保留 `original_path`，其值固定为 `null`；图片定位使用 `source_relative_path`，云端对象定位使用 `oss_path`。

### 6.2 与商品的关系

```text
Product 1 ───── 0..N ImageAsset
ImageAsset ──── 0..1 Product
```

- 未归款图片的 `model_number = NULL`
- 删除商品只解除关联，不删除或归档图片资产
- 当前商品 CRUD 上传图片时也应走同一个图片资产入库服务，避免形成第二套图片仓库
- `products` 必填字段放宽和 `draft/complete` 状态属于已确认的下一阶段，不在本期修改

### 6.3 旧 `product_images`

当前开发库已核对为 0 行，但实施前必须重新检查。

安全迁移策略：

1. 先创建 `image_assets`
2. 所有新写入与搜索切换到 `image_assets`
3. 若实施时 `product_images` 非空，停止自动清理并生成兼容迁移方案
4. 验证新路径后，旧表仅标记为废弃
5. 删除旧表必须另行执行并再次确认，不能藏在应用启动中

## 7. OSS 对象布局

### 7.1 原图

```text
image-search/xiangxipackage/<source_relative_path>
```

要求：

- 保持原始相对路径和文件内容
- Bucket 继续保持私有
- 上传时设置正确 `Content-Type`
- 元数据记录源提供方、源 Bucket、SHA-256 和原始大小
- 已存在对象必须 HEAD 校验，不能默认覆盖

### 7.2 搜索预览图

```text
image-search/previews/preview-v1/<sha256前2位>/<sha256>.jpg
```

同一内容哈希复用同一个预览对象。永久清除某项资产时，只有确认没有其他资产引用后，才能删除共享预览图。

### 7.3 私有访问

数据库只保存 Object Key，不保存公开 URL 或临时签名 URL。

建议接口：

```text
GET /api/image-assets/<asset_id>/preview
```

后端查出 `preview_oss_path`，生成短时签名 URL 并返回 `302`。签名时间由配置控制；日志不得记录完整签名参数。

缩略图如需进一步缩放，将 `x-oss-process` 参数纳入签名，而不是签名后拼接。

## 8. 图片标准化

### 8.1 搜索预览图规范

- 应用 EXIF 方向
- 保持宽高比
- 不放大小图
- 最长边不超过 2048 px
- 输出 JPEG
- 透明图片在明确的白色背景上合成
- 动图使用首个有效画面
- 自适应调整尺寸和质量，最终文件必须不超过 2.5 MiB
- 如果多轮处理后仍无法满足约束，明确失败，绝不把超限内容发送给 embedding API

标准化实现必须是确定且带版本的；参数改变时增加 `normalization_version`，避免旧向量与新向量来源不可追踪。

### 8.2 入库图片

源图不改变。搜索预览图持久化到 OSS，embedding 使用该预览图生成。

### 8.3 查询图片

浏览器只做“传输预处理”：

- 超大文件或超大像素图片先缩小，确保请求低于后端 16 MiB 限制
- 不把浏览器输出视为最终 embedding 输入

后端再调用与入库一致的 `ImageNormalizer`，得到规范化查询图后生成 embedding。

查询图：

- 不上传 OSS
- 不写数据库
- 使用安全临时文件
- 成功、失败和异常路径都必须清理

## 9. Embedding

- 模型：`tongyi-embedding-vision-plus-2026-03-06`
- 维度：1024
- 入库图和查询图必须使用相同模型与相同后端标准化规则
- 单次批量最多 20 张
- 429 指数退避逻辑复用现有实现
- 批失败时，除账号级限流外，可降级逐张定位坏图
- 不允许在同一个 HNSW 索引中混用不同模型或维度

### 9.1 相同内容复用

查到相同 `content_hash` 且以下版本均一致时：

- `embedding_model`
- `embedding_dimension`
- `normalization_version`

可以复用 `preview_oss_path` 和 vector，但仍创建新的 `image_assets` 行并保留新的来源相对路径。

## 10. 图片级检索

检索不再过采样后按型号折叠，目标 SQL 语义为：

```sql
SET LOCAL hnsw.ef_search = :ef_search;

SELECT id,
       model_number,
       source_relative_path,
       oss_path,
       preview_oss_path,
       vector <=> CAST(:query_vector AS vector) AS distance
FROM image_assets
WHERE status = 'active'
ORDER BY vector <=> CAST(:query_vector AS vector)
LIMIT :top_k;
```

- `ef_search = max(top_k, 40)`
- `similarity = min(1.0, max(0.0, 1.0 - distance))`
- 不使用 `DISTINCT ON (model_number)`
- 不要求 join `products`
- 搜索结果顺序必须与 SQL 距离顺序一致

建议响应：

```json
[
  {
    "asset_id": "uuid",
    "model_number": null,
    "relative_path": "2025.4.18海报照片/某目录/IMG_001.jpg",
    "preview_url": "/api/image-assets/uuid/preview",
    "similarity": 0.913
  }
]
```

## 11. Kodo → OSS 迁移

### 11.1 命令

建议新增：

```bash
cd backend
python -m scripts.migrate_kodo_to_oss --dry-run
python -m scripts.migrate_kodo_to_oss --pilot 10
python -m scripts.migrate_kodo_to_oss --full
```

可选参数：

- `--prefix`
- `--limit`
- `--batch-size`，限制在 1–20
- `--report-path`
- `--retry-failed <previous-report.json>`

`--full` 与 `--pilot` 互斥。没有显式模式时只允许 dry-run，避免误操作。

### 11.2 单张流程

1. 列举 Kodo 对象
2. 过滤支持的图片类型
3. 将 Kodo Key 原样记为 `source_relative_path`
4. 下载到专用临时目录
5. 流式计算 SHA-256
6. 验证真实图片类型、尺寸和可解码性
7. 检查来源唯一键是否已存在
8. 检查 OSS 原图对象
9. 上传或校验原图
10. 复用或生成搜索预览图
11. 复用或批量生成 embedding
12. 事务写入 `image_assets`
13. 清理临时文件

### 11.3 幂等与冲突

| 状态 | 行为 |
|---|---|
| 同来源路径、同 SHA-256、数据库已存在 | 跳过 |
| 同来源路径、不同 SHA-256 | `source_conflict`，不覆盖 |
| 不同来源路径、同 SHA-256 | 新建独立资产，复用预览与向量 |
| OSS 已存在且元数据一致 | 跳过上传，继续后续阶段 |
| OSS 已存在但校验不一致 | 冲突，不覆盖 |
| OSS 已上传但 embedding/DB 失败 | 保留对象；重跑继续修复缺失阶段 |

迁移不依赖单独 checkpoint；OSS 与 PostgreSQL 的可验证状态就是断点。报告用于审计与定向重试。

### 11.4 报告

JSON 报告至少包括：

- 扫描对象数、图片数、非图片数
- 下载成功/失败
- 原图上传/已存在/冲突
- 预览生成/复用/失败
- embedding 生成/复用/失败
- 数据库新增/已存在/冲突
- 每个失败对象的来源相对路径、失败阶段和脱敏错误
- 总字节数与耗时

终端只显示汇总和有限数量示例，完整明细写报告文件。

## 12. 试迁移与全量门槛

### 12.1 10 张试迁移样本

样本必须覆盖：

- 中文和空格路径
- 多层目录
- 普通 JPEG/PNG/WebP 中实际存在的格式
- 一张超过 20 MiB 的图片
- 一组内容相同、路径不同的图片
- 一张较小图片，验证不会被放大

### 12.2 试迁移通过条件

1. OSS 原图内容哈希与来源一致
2. 搜索预览图可访问且方向、比例正确
3. 每个来源路径有独立 `image_assets` 记录
4. 重复内容的两条记录均存在，且复用预览/向量
5. `model_number` 为空
6. 查询样本能返回正确相对路径
7. 私有 OSS 未暴露永久公开 URL
8. 重跑不重复上传、生成向量或插入记录
9. 来源冲突不会覆盖
10. 报告可对账且不包含凭证

试迁移完成后暂停，由用户确认结果，再运行全量迁移。

### 12.3 全量通过条件

- 迁移前重新扫描并锁定预期图片数量
- `成功 + 已存在 + 明确失败 = 预期图片数`
- OSS 原图对象数、总字节和抽样哈希可对账
- 活跃 `image_assets` 数等于成功导入的来源路径数
- 失败项全部有路径和阶段
- 抽查普通图、超大图、中文路径和重复内容搜索
- Kodo 无任何删除或改写

## 13. 错误处理与安全

- 凭证只从环境变量读取，不在命令参数、日志、报告或数据库中出现
- 签名 URL 不持久化
- Kodo 列举失败时不开始写入
- OSS Bucket、地域和前缀预检失败时不开始 embedding
- 数据库不可达时不开始全量迁移
- 单图失败不阻塞后续图片
- 数据库事务以小批次提交，失败批次可重跑
- 所有临时目录使用专用随机目录并确保 finally 清理
- 图片解码需要像素上限和损坏图片处理，防止压缩炸弹
- 私有 Bucket 保持私有，不为方便展示改成公共读

## 14. 配置

### 14.1 `backend/.env.example`

补齐并统一变量名：

```dotenv
# Qiniu source
QINIU_ACCESS_KEY=
QINIU_SECRET_KEY=
QINIU_BUCKET_NAME=xiangxipackage
QINIU_REGION=z0

# Aliyun OSS target
OSS_ACCESS_KEY_ID=
OSS_ACCESS_KEY_SECRET=
OSS_ENDPOINT=
OSS_BUCKET_NAME=
OSS_IMAGE_BASE_PREFIX=image-search
OSS_SIGNED_URL_TTL_SECONDS=600

# Image normalization
IMAGE_PREVIEW_MAX_EDGE=2048
IMAGE_PREVIEW_MAX_MB=2.5
IMAGE_NORMALIZATION_VERSION=preview-v1
```

迁移脚本可在过渡期兼容现有七牛变量别名，但只记录“使用了兼容别名”，不得输出值。

### 14.2 Docker Compose

后端容器应通过 `env_file: ./backend/.env` 读取应用密钥，同时保留 Compose 中显式的容器数据库地址覆盖，避免 `DB_HOST=localhost` 在容器内生效。

必须继续保证：

- `.env` 不进入镜像
- `.env` 不被 Git 跟踪
- 健康检查不输出配置

## 15. 前端

### 15.1 查询图

新增浏览器端传输预处理工具：

- 文件和像素数检查
- EXIF 方向兼容
- 大图缩小到可安全上传的中间尺寸
- 输出小于后端请求上限
- 失败时显示明确提示

后端仍执行最终标准化，前端结果不能直接作为可信 embedding 输入。

### 15.2 搜索结果

新增图片资产结果类型，字段不再强制要求完整 Product：

```ts
type ImageAssetSearchResult = {
  asset_id: string
  model_number: string | null
  relative_path: string
  preview_url: string
  similarity: number
}
```

卡片显示：

- OSS 搜索预览图
- 相似度
- 完整来源相对路径
- 有型号时显示型号；否则显示“未补充型号”
- 复制相对路径按钮

不得因为 `model_number` 为空而过滤结果。

## 16. 文件级变更清单

### 16.1 新增

| 文件 | 内容 |
|---|---|
| `backend/models/image_asset.py` | `ImageAsset` ORM 模型 |
| `backend/services/image_normalizer.py` | 入库与查询共用标准化 |
| `backend/services/object_storage.py` | 私有 OSS 操作与签名 |
| `backend/blueprints/image_assets.py` | 预览与图片资产相关 API |
| `backend/scripts/migrate_kodo_to_oss.py` | Kodo → OSS → pgvector 迁移 |
| `backend/test/test_image_normalizer.py` | 尺寸、方向、透明、大小保证 |
| `backend/test/test_asset_ingest.py` | 重复内容、冲突、复用与幂等 |
| `backend/test/test_kodo_oss_migration.py` | 迁移编排和报告 |
| `backend/test/integration/test_image_asset_search.py` | 真 PostgreSQL 图片级检索 |
| `frontend/src/utils/prepareSearchImage.ts` | 查询图传输预处理 |

领域文档和 ADR 已在访谈阶段新增。

### 16.2 修改

| 文件 | 修改 |
|---|---|
| `backend/models/__init__.py` | 导出 `ImageAsset` |
| `backend/models/product.py` | 移除图片资产的所有权/级联删除假设 |
| `backend/services/embedding.py` | 使用标准化结果；补最终大小校验 |
| `backend/services/ingest.py` | 从本地产品图片入库改为独立 OSS 图片资产入库 |
| `backend/services/vector_search.py` | 查询 `image_assets`，移除型号折叠 |
| `backend/product_search.py` | 继续维持兼容导出 |
| `backend/blueprints/products_v2.py` | 搜索返回图片资产；现有商品图片写入走统一服务 |
| `backend/app.py` | 注册图片资产蓝图和存储服务配置 |
| `backend/init_db.py` | 创建并收敛 `image_assets` 与索引；不暗中删除旧表 |
| `postgres/init/01_init.sql` | 新库使用 `image_assets` |
| `backend/.env.example` | Kodo、OSS、预览和签名配置 |
| `docker-compose.yml` | 安全加载 `backend/.env` |
| `frontend/src/types/product.ts` | 增加独立图片搜索结果类型 |
| `frontend/src/services/productApi.ts` | 适配图片资产搜索响应 |
| `frontend/src/components/ProductSearch.tsx` | 大图预处理、无型号卡片、相对路径 |
| `frontend/nginx.conf` | 明确请求大小策略，与前端预处理及 Flask 保持一致 |
| `backend/test/test_products_v2_search_behaviors.py` | 响应契约改为图片资产，覆盖空型号 |
| `backend/test/integration/conftest.py` | 增加 `image_assets` 测试工厂和清理 |
| `backend/test/integration/test_schema.py` | 校验新表、非唯一哈希和可空型号 |
| `backend/test/integration/test_vector_search.py` | 从型号折叠断言改为图片级排序 |
| `backend/test/integration/test_logging.py` | 保留真实搜索日志覆盖并适配新表 |
| `backend/test/integration/test_write_paths.py` | OSS 资产写入替代本地产品图片写入 |

### 16.3 停用或删除

| 文件/逻辑 | 原因 |
|---|---|
| `backend/scripts/ingest_images.py` | 当前依赖“一级目录=型号”和本地落盘；本期由 Kodo 迁移 CLI 取代，未来文件夹上传另行设计 |
| `backend/scripts/migrate_oss_path.py` | 实际生成七牛公开 URL，语义错误 |
| `backend/scripts/README_OSS_MIGRATION.md` | 描述旧的本地路径 → 七牛 URL 流程 |
| `backend/blueprints/oss.py` 中公开 URL 拼接 | Bucket 为私有且该蓝图未注册；不得直接复用旧公开访问语义 |
| `product_images.content_hash` 全库唯一规则 | 不同路径的相同内容必须保留为不同资产 |
| `DISTINCT ON (model_number)` | 与图片级结果冲突 |
| 本地 uploads 作为正式图片源 | OSS 已成为正式图片源 |
| 公开 OSS URL 拼接 | Bucket 私有且签名会过期 |

停用不等于立即物理删除；实施时先核对调用引用，再做最小安全清理。

## 17. 测试策略

### 17.1 单元测试

- 超大、超像素、透明、带 EXIF、损坏图片
- 最长边、比例、不放大和 2.5 MiB 硬保证
- 相同哈希复用预览与向量
- 相同哈希不同路径分别建资产
- 同路径同内容跳过
- 同路径不同内容报告冲突
- 签名 URL 不入库、不入日志
- 任意异常路径清理临时文件

### 17.2 PostgreSQL 集成测试

- `model_number = NULL` 可检索
- 同型号多图片分别返回
- 同哈希多图片分别返回
- 归档图片不返回
- 相似度限制在 `[0, 1]`
- HNSW `ef_search` 在事务结束后不污染连接池
- 删除 Product 后资产保留且型号置空

### 17.3 API 与前端

- 16 MiB 以上的浏览器原图经过传输预处理后可搜索
- 无型号结果不被过滤
- 中文相对路径完整显示和复制
- 私有预览 302 可被 `<img>` 正常跟随
- 空结果、损坏图片、413、embedding 503 文案明确
- 前端类型检查与生产构建通过
- 浏览器真实上传与搜索留存截图证据

## 18. 实施顺序

实施计划应按 TDD 拆为以下阶段：

1. 数据模型与 PostgreSQL 集成测试
2. 图片标准化及其单元测试
3. OSS 存储与私有预览
4. 独立资产入库服务
5. 图片级检索 API
6. 前端大图处理与结果卡片
7. Kodo → OSS 迁移 CLI
8. 10 张真实试迁移与搜索验收
9. 用户确认后全量迁移
10. 全量对账与最终搜索抽查

任何真实对象写入、数据库迁移或全量 embedding 调用，都必须发生在代码测试通过之后。

## 19. 与旧设计的关系

本设计取代 `2026-07-28-image-search-refactor-design.md` 中以下决定：

- “目录名即型号”
- “图片必须属于现有 Product”
- “content_hash 全库唯一”
- “按 model_number 折叠搜索结果”
- “本地文件系统是正式图片源”

旧设计中仍保留的能力：

- DashScope 模型与 1024 维向量
- pgvector/HNSW
- 批量最多 20 张
- 429 重试
- `SET LOCAL hnsw.ef_search`
- 相似度上下界夹紧

`2026-07-28-unified-search-design.md` 的文本搜索不在本期；未来实施时必须改用图片资产结果契约，不能继续假定型号和完整 Product 必然存在。

## 20. 后续阶段

本期完成后再单独设计：

- 单张手工补型号与商品资料
- 待完善商品状态
- Excel 导出、编辑和导回
- `asset_id` 优先、相对路径兜底的匹配
- 空白字段与显式清空语义
- 图库文件夹拖拽上传
- 按型号折叠的可选展示模式
- 永久清除与共享预览引用检查
