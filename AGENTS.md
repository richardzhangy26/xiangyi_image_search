# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Qoder, etc.) when working with code in this repository.

## 项目概览

**电子产品配件图像搜索系统 (Electronic Accessories Image Search System)** 面向跨境电商卖家和批发采购，使用通义多模态 embedding 与 PostgreSQL/pgvector 完成以图搜款。

- 用户上传一张图片后，系统从 image_assets 检索视觉相似的图片资产。
- **OSS 已成为正式图片源**：原图和规范化预览都存放在私有 Aliyun OSS 的隔离前缀中。
- Kodo 是迁移期间的**Kodo 只读备份**，只能由受控迁移命令读取，不能作为公开 URL 来源。
- PostgreSQL 保存商品元数据、图片资产元数据和向量；凭证只保留在本地环境文件中。

## 架构

### 后端 (Flask + Python)

- Flask + Flask-SQLAlchemy。
- PostgreSQL 16 + pgvector（数据库 image_search），由 Docker 提供。
- DashScope tongyi-embedding-vision-plus-2026-03-06 生成 1024 维向量；向量在数据库内按余弦距离检索。
- 无进程内向量索引，服务重启不会丢失检索数据。

### 正式图片工作流

1. scripts.migrate_kodo_to_oss 对 Kodo 做只读 preflight、盘点和经授权的迁移；Kodo 不执行 Put/Delete。
2. 迁移或产品上传经过 ImageAssetIngestService：在私有 OSS 中无覆盖写入原图和 preview-v1 预览，然后写入 image_assets 及其向量。
3. VectorSearchService 只检索 image_assets.status = active 的向量。
4. API 只返回资产 ID 和 /api/image-assets/<asset_id>/preview；该入口生成短时签名 URL 并返回私有 302，不保存或拼接公开 URL。

product_images 是**未修改的退休兼容表**，不属于新库 schema、应用 ORM 或活动写路径。任何另行授权的兼容迁移前，先运行 python -m scripts.audit_legacy_product_images，根据只读审计结果制作人工迁移清单；本仓库不自动迁移、删除或覆盖它，也不物理清理旧对象。

### 关键文件

- backend/app.py - Flask 初始化、数据库配置、CORS、蓝图注册和日志。
- backend/models/image_asset.py - 独立 ImageAsset 模型；型号可空，商品删除时设为 NULL。
- backend/services/image_normalizer.py - EXIF、尺寸、透明背景、动图首帧和 2.5 MiB 上限。
- backend/services/object_storage.py - 私有 OSS HEAD、无覆盖上传和短时签名下载。
- backend/services/asset_ingest.py - 来源图片到 OSS、embedding 和 PostgreSQL 的纵向入库服务。
- backend/services/vector_search.py - pgvector 检索服务。
- backend/services/kodo_source.py - Kodo S3 只读对象来源。
- backend/scripts/migrate_kodo_to_oss.py - 受控 Kodo → 私有 OSS 迁移入口。
- backend/scripts/audit_legacy_product_images.py - 独立、只读的退休兼容表审计入口。
- backend/blueprints/products_v2.py - Product CRUD、CSV 导入和产品图片资产入库（/api/products）。
- backend/blueprints/image_assets.py - 图片资产列表、归款和私有预览 302（/api/image-assets）。
- backend/models/product.py - Product 商品元数据模型。
- backend/init_db.py - 创建 pgvector 扩展、模型表和 image_assets HNSW 索引。
- postgres/init/01_init.sql - Docker 首次启动 SQL；应与模型保持一致。

### Embedding 模型

| 项目 | 值 |
| --- | --- |
| Model | tongyi-embedding-vision-plus-2026-03-06（Qwen3 base） |
| Dimension | 1024（数据库列为 vector(1024)） |
| Metric | Cosine distance |
| Text support | 30+ 语言；未来可用同一向量空间做文本搜图 |

不要把不同模型的向量混入同一张资产表。更换模型时必须重新生成全部资产向量。

## 向量搜索实现

**核心类**：VectorSearchService（backend/services/vector_search.py）。

~~~
search_similar_images(image_path, top_k=10, request_id=None) -> list
search_by_vector(vector, top_k=10, request_id=None) -> list
~~~

查询只读活跃资产，并在事务内设置 HNSW 搜索参数；每个结果包含 asset_id、可选 model_number、来源相对路径、相似度和私有预览入口。相似度按 min(1.0, max(0.0, 1.0 - distance)) 返回。

~~~
SET LOCAL hnsw.ef_search = :ef;
SELECT id, model_number, source_relative_path,
       vector <=> CAST(:query_vector AS vector) AS distance
FROM image_assets
WHERE status = 'active'
ORDER BY vector <=> CAST(:query_vector AS vector)
LIMIT :top_k;
~~~

SET LOCAL 保证连接归还连接池时不污染后续请求；服务在成功和异常路径都会 rollback。

## 前端

- React 18 + TypeScript + Vite；Ant Design 5 和 Tailwind CSS。
- ProductSearch（以图搜款）和 ProductUpload（产品管理）是当前路由组件。
- ProductUpload 默认展示待归款图片，支持按来源路径搜索、分页、多选并关联既有型号；不自动推断或创建型号，也不提供解绑、跨型号改绑。
- 前端通过 /api/ 访问后端；Nginx 只代理 API，不提供本地图片静态源。
- 图片卡片使用私有预览入口，浏览器跟随 302 获取短时签名地址。

## Docker 部署

docker-compose.yml 包含 db、backend、frontend 三个服务，网络为 app-network：

| 服务 | 容器 | 端口 | 说明 |
| --- | --- | --- | --- |
| db | fashion-crm-db | 127.0.0.1:5433 → 5432 | pgvector PostgreSQL 16 |
| backend | fashion-crm-backend | 0.0.0.0:5000 → 5000 | Gunicorn；健康检查 /api/health |
| frontend | fashion-crm-frontend | 0.0.0.0:80 → 80 | Nginx 静态构建和 API 代理 |

- 数据库卷 postgres_data 保存 PostgreSQL 数据。
- 后端只挂载 ./backend/data:/app/data 供运行时数据使用；正式图片不落盘到容器文件系统，原图和预览均在私有 OSS。
- postgres/init/*.sql 只在空数据库首次启动时执行，创建扩展、商品表、image_assets 和 HNSW 索引。
- docker compose down -v 会删除数据库卷和向量，执行前必须确认已有备份；不执行旧图片对象的物理清理。

~~~
docker compose up -d
docker compose build backend
docker compose build frontend && docker compose up -d frontend
docker compose logs -f backend
docker compose down
~~~

## 备份与恢复

~~~
docker exec fashion-crm-db pg_dump -U postgres image_search > backup_$(date +%Y%m%d).sql
~~~

数据库备份覆盖商品、image_assets 元数据和向量。OSS 原图、预览及 Kodo 只读备份由对象存储侧的私有备份/版本策略负责；不要在应用脚本中删除、覆盖或公开它们。恢复时先恢复 PostgreSQL，再核对 OSS 对象和 image_assets 的来源绑定。

## 本地开发

~~~
cd backend
cp .env.example .env       # 填写凭证和数据库连接
python init_db.py
python app.py              # http://0.0.0.0:5000

cd ../frontend
npm install
npm run dev                # http://localhost:5173
~~~

数据库连接优先读取 DATABASE_URL，否则组合 DB_HOST、DB_PORT、DB_NAME、DB_USER、DB_PASSWORD。

## 环境变量

backend/.env（由 .env.example 复制）中的必填项：

- DASHSCOPE_API_KEY - DashScope embedding 凭证。
- DB_HOST、DB_PORT、DB_NAME、DB_USER、DB_PASSWORD - PostgreSQL 连接。
- OSS_ACCESS_KEY_ID、OSS_ACCESS_KEY_SECRET、OSS_ENDPOINT、OSS_BUCKET_NAME - 私有 OSS 原图/预览和签名下载。
- QINIU_ACCESS_KEY、QINIU_SECRET_KEY、QINIU_BUCKET_NAME、QINIU_REGION - **Kodo 只读迁移来源**，仅供 migrate_kodo_to_oss 的 preflight/迁移读取，绝不生成公开 URL。

可选项：DATABASE_URL、QINIU_S3_BUCKET_NAME、OSS_IMAGE_BASE_PREFIX、OSS_SIGNED_URL_TTL_SECONDS、IMAGE_PREVIEW_MAX_EDGE、IMAGE_PREVIEW_MAX_MB、IMAGE_MAX_PIXELS、IMAGE_NORMALIZATION_VERSION、SEARCH_OVERSAMPLE、LOG_LEVEL。Docker Compose 会把容器内的数据库地址覆盖为 db:5432。

## 数据库结构

**数据库**：image_search · **扩展**：vector

### products

主键为 model_number VARCHAR(100)；保存摄影师文件、阿里商品 URL、分类、规格、价格、参考链接以及创建/更新时间。CSV 导入只创建商品元数据。

### image_assets

| 字段 | 说明 |
| --- | --- |
| id | UUID 主键 |
| model_number | 可空外键；未归款资产为 NULL |
| source_provider / source_bucket / source_relative_path / source_revision | 来源身份和修订号 |
| oss_path / preview_oss_path | 私有 OSS 原图和规范化预览对象键 |
| content_hash / source_size / source_mime_type / source_width / source_height | 原图内容和尺寸元数据 |
| vector / embedding_model / embedding_dimension | 1024 维 pgvector 及模型绑定 |
| normalization_version / status | 规范化版本；active 或 archived |

索引包括内容哈希、型号、状态和只针对 active 资产的 HNSW cosine 索引。退休兼容表保持原样，不由新库初始化或应用写路径创建；如需兼容迁移，先单独执行只读审计并取得授权。

## 产品与迁移操作

POST /api/products/import-csv 接受 UTF-8、GBK、GB2312 或 UTF-8-SIG CSV，必填列为 model_number、photographer_file、alibaba_product_url、category。产品图片通过产品 API 写入私有 OSS 和 image_assets，不会产生公开对象地址。

Kodo 迁移在 backend 目录运行：

~~~
python -m scripts.migrate_kodo_to_oss --preflight
python -m scripts.migrate_kodo_to_oss --dry-run --report-path reports/kodo-dry-run.json
python -m scripts.migrate_kodo_to_oss --verify-selection --selection-manifest reports/issue-10/selection.json --report-path reports/issue-10/selection-verification.json
python -m scripts.migrate_kodo_to_oss --pilot 10 --selection-manifest reports/issue-10/selection.json --verified-selection-report reports/issue-10/selection-verification.json --report-path reports/kodo-pilot.json
~~~

不提供 --pilot 或 --full 时命令只读；全量写入还需要受控授权、数据库恢复点和新鲜 preflight/dry-run 报告。迁移报告包含对象统计、阶段状态、冲突和脱敏失败原因，不保存凭证或签名 URL。兼容审计是另一条命令，不能用迁移命令替代：

~~~
python -m scripts.audit_legacy_product_images
~~~

审计只检查退休表是否存在及行数：表不存在或为空表示没有待处理的旧行；非空表示必须由人工制定、单独批准的兼容迁移清单。审计不会扫描文件、写入数据库、上传 OSS、删除表或删除对象。

## 测试

~~~
cd backend
python -m pytest test/ -v
python -m pytest test/integration/ -v
python -m pytest test/ --ignore=test/integration -v
~~~

集成测试使用 DB_HOST:DB_PORT（默认 localhost:5433）上的独立 image_search_test 数据库；连接不可用时会自动 skip。test/test_pgvector.py 和 test/benchmark_search.py 是手工基准脚本，不是 pytest 用例。

## 重要约束

- 不执行 DROP、DELETE、覆盖上传或云对象清理来“迁移”旧数据；所有收缩动作必须另行授权并可回滚。
- 图片正式来源始终是私有 OSS，Kodo 只读备份；预览始终经过 /api/image-assets/<asset_id>/preview 的短时签名 302。
- 不在应用启动、普通部署或健康检查中隐式运行兼容审计或迁移。
- Legacy TypeScript 组件（如 OrderManagement）未路由且可能有类型错误，除非重新启用，否则不要顺手修复。
- 如果 Docker 报告历史容器名称冲突，只处理明确冲突的容器；不要删除数据库卷。
- 完成稳定功能后，仅在改变架构事实、入口或操作约束时更新最近作用域的 AGENTS.md；记录当前事实，不记录实现过程。

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues (richardzhangy26/xiangyi_image_search) via the gh CLI. See docs/agents/issue-tracker.md.

### Triage labels

Default triage vocabulary: needs-triage / needs-info / ready-for-agent / ready-for-human / wontfix. See docs/agents/triage-labels.md.

### Domain docs

Single-context: one CONTEXT.md + docs/adr/ at the repo root. See docs/agents/domain.md.
