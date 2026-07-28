# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Qoder, etc.) when working with code in this repository.

## Project Overview

**电子产品配件图像搜索系统 (Electronic Accessories Image Search System)** - AI-powered image search and product management system designed for camera straps, lanyards, and other electronic product accessories. Implements reverse image search (以图搜图) using Tongyi multimodal embeddings and PostgreSQL pgvector similarity search.

**Key Business Context**:
- Target users: Cross-border e-commerce sellers, wholesale buyers
- Use case: Quickly find products matching customer inquiry images
- Core feature: Upload an image → find visually similar products in database
- Data ownership: Fully local deployment (local PostgreSQL + local image storage) for security and privacy

## Architecture

### Backend (Flask + Python)
- **Framework**: Flask with Flask-SQLAlchemy ORM
- **Database**: Local PostgreSQL 16 + pgvector extension (database: `image_search`)
  - Runs in Docker via `pgvector/pgvector:pg16` image
  - ⚠️ Supabase / MySQL / SQLite / FAISS are all **fully removed** (July 2026 migration)
- **AI Pipeline**:
  - DashScope `tongyi-embedding-vision-plus-2026-03-06` model (Qwen3-based) generates **1024-dim** vectors
  - Model + dimension defined as constants `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION` in [backend/services/embedding.py](backend/services/embedding.py)（`backend/product_search.py` 兼容层再导出同名符号）
  - pgvector performs in-database cosine similarity search with HNSW index
  - **Stateless architecture**: No in-memory index, vectors stored natively in PostgreSQL
- **Storage**: Local filesystem (`backend/uploads/product_images/{model_number}/`), bind-mounted into Docker
  - Aliyun OSS scripts exist (`backend/scripts/batch_upload_oss.py`) but OSS is **not used** by default; migrate only if public/multi-site access is needed
- **Dev environment**: miniconda `base` env (`~/miniconda3/bin/python`), Python 3.9+

**Critical Files**:
- [backend/app.py](backend/app.py) - Flask app init, DB config (`DATABASE_URL` or `DB_*` vars), CORS, blueprint registration, root logger config (`logging.basicConfig`)
- [backend/services/embedding.py](backend/services/embedding.py) - `EmbeddingClient`: DashScope 调用，单张 + 批量（**硬上限 20 张/请求**）、429 重试、批失败降级为单张
- [backend/services/vector_search.py](backend/services/vector_search.py) - `VectorSearchService`: pgvector 检索，SQL 内过采样 + `DISTINCT ON` 按型号折叠
- [backend/services/ingest.py](backend/services/ingest.py) - `ImageIngestService`: SHA-256 全库去重 + 哈希命名落盘 + 入库，CLI 与 API 共用
- [backend/product_search.py](backend/product_search.py) - 兼容层，仅再导出 `services/` 中的符号（`ImageSearchService = VectorSearchService`）
- [backend/scripts/ingest_images.py](backend/scripts/ingest_images.py) - 目录批量导入 CLI
- [backend/blueprints/products_v2.py](backend/blueprints/products_v2.py) - Product CRUD API (the only active blueprint, prefix `/api/products`)
- [backend/models/product.py](backend/models/product.py) - `Product` / `ProductImage` SQLAlchemy models (pgvector `Vector(1024)`)
- [backend/init_db.py](backend/init_db.py) - One-shot init: `CREATE EXTENSION vector` + `create_all()` + HNSW index
- [postgres/init/01_init.sql](postgres/init/01_init.sql) - Docker auto-init SQL (extension + tables + HNSW index); keep in sync with models

### Embedding Model (Tongyi / 通义千问)

| Item | Value |
|------|-------|
| Model | `tongyi-embedding-vision-plus-2026-03-06` (Qwen3 base) |
| Dimension | 1024 (via `dimension` param; must match DB `vector(1024)` column) |
| Metric | Cosine distance (embeddings normalized) |
| Image cost | ~402 tokens/image at default `res_level=1` → ≈¥0.0002/image (0.0005元/千token) |
| Text support | 30+ languages, same vector space as images → **text-to-image search works** |
| Fusion vectors | Supported by putting text+image in one content object (future: image+instruction queries) |

**Key facts**:
- Text and image vectors share one embedding space: a future natural-language product search endpoint only needs to embed the query text with the **same model** and reuse the existing cosine SQL query.
- Never mix vectors from different models in one table — vector spaces are incompatible. Changing the model requires regenerating all vectors.
- Cross-modal (text↔image) similarity values are much lower than image↔image; rank by top-k, do not apply absolute thresholds.
- Legacy `multimodal-embedding-v1` was replaced (more expensive, weaker text-image alignment).

**Vector Search Workflow**:
1. **Indexing**: Image upload → DashScope API (base64 Data URI, auto-compressed to ≤2.5MB JPEG) → 1024-dim vector → `product_images.vector`
2. **Search**: Query image → embedding → `VectorSearchService.search_by_vector` 执行 SQL 内过采样 + `DISTINCT ON` 折叠（见下方 SQL Query Pattern）→ top-k
3. Similarity score returned to clients: `min(1.0, max(0.0, 1.0 - cosine_distance))`

### Frontend (React + TypeScript)
- **Framework**: React 18 + TypeScript, Vite (dev server `localhost:5173`)
- **UI**: Ant Design 5 + Tailwind CSS
  - Global antd theme via `ConfigProvider` in [frontend/src/main.tsx](frontend/src/main.tsx): teal primary `#0d7a72`, amber accent `#d97b29`, paper background `#f6f4ef`, zh_CN locale
  - Design language: "warm paper workbench" — noise-textured paper background, grouped toolbar, contextual batch-action bar, drag-and-drop CSV upload (see [frontend/src/index.css](frontend/src/index.css))
- **Active components**: `ProductSearch` (以图搜款), `ProductUpload` (产品管理). Other components (orders/customers) are legacy and not routed.
- API base URL resolves from `window.location.hostname` ([frontend/src/config.ts](frontend/src/config.ts)) — frontend calls backend on port 5000 directly; nginx also proxies `/api/` and `/uploads/` as same-origin fallback.

## Docker Deployment (Current)

Three services in [docker-compose.yml](docker-compose.yml), network `app-network`:

| Service | Image | Container | Ports | Notes |
|---------|-------|-----------|-------|-------|
| `db` | `pgvector/pgvector:pg16` | fashion-crm-db | `127.0.0.1:5433→5432` | Host port **5433** to avoid clashing with a local PostgreSQL on 5432 |
| `backend` | built from `backend/Dockerfile` | fashion-crm-backend | `0.0.0.0:5000→5000` | Gunicorn 4 workers; healthcheck `GET /api/health` |
| `frontend` | built from `frontend/Dockerfile` | fashion-crm-frontend | `0.0.0.0:80→80` | Nginx serves Vite build, proxies `/api/` + `/uploads/` to backend |

- **DB auto-init**: `postgres/init/*.sql` mounted to `/docker-entrypoint-initdb.d` — runs only on first startup of an empty `postgres_data` volume (extension, tables, HNSW index `m=16, ef_construction=64`).
- **Volumes**: `postgres_data` (database), `./backend/uploads` and `./backend/data` bind mounts.
- **Env**: root `.env` supplies `DB_NAME` / `DB_USER` / `DB_PASSWORD` / `DASHSCOPE_API_KEY` to compose.
- **nginx gotcha**: `proxy_pass http://backend:5000;` (no trailing slash/path!) — backend routes already include the `/api` prefix; a trailing `/` would strip it and 404.
- **CORS** is fully open (`origins: "*"`) — internal LAN tool.

```bash
docker compose up -d              # start all (db → backend → frontend, healthcheck-gated)
docker compose build backend      # rebuild after backend code changes
docker compose build frontend && docker compose up -d frontend   # redeploy frontend
docker compose logs -f backend
docker compose down               # stop; data persists in volumes
docker compose down -v            # ⚠️ deletes postgres_data (vectors lost)
```

**Backup**:
```bash
docker exec fashion-crm-db pg_dump -U postgres image_search > backup_$(date +%Y%m%d).sql
tar -czf uploads_backup_$(date +%Y%m%d).tar.gz backend/uploads/
```

## Local Development (without Docker for app code)

```bash
# Backend (uses miniconda base env; deps already installed)
cd backend
cp .env.example .env      # set DASHSCOPE_API_KEY; DB points to localhost:5433 (dockerized PG)
python init_db.py         # extension + tables + HNSW index (idempotent)
python app.py             # http://0.0.0.0:5000

# Frontend
cd frontend
npm install
npm run dev               # localhost:5173

# Tests
cd backend && python -m pytest test/ -v
```

DB connection resolution in `app.py`: `DATABASE_URL` (full DSN, optional) → else `DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD` (defaults target local PostgreSQL).

## Environment Variables

`backend/.env` (from `.env.example`):

**Required**:
- `DASHSCOPE_API_KEY` - Aliyun DashScope API key (⚠️ embedding calls fail with "Access denied / overdue payment" if the Aliyun account balance is empty)
- `DB_HOST=localhost`, `DB_PORT=5433`, `DB_NAME=image_search`, `DB_USER=postgres`, `DB_PASSWORD` - local dockerized PostgreSQL

**Optional**:
- `DATABASE_URL` - full PostgreSQL DSN, overrides `DB_*`
- `OSS_*` - Aliyun OSS (unused by default)
- `DATASET_ROOT` - local dataset path
- `SEARCH_OVERSAMPLE` - 检索过采样系数，默认 5（≈ 单型号平均图片数）。产品图片数普遍偏多时调大
- `LOG_LEVEL` - root logger 级别，默认 `INFO`（见 `backend/app.py` 的 `logging.basicConfig`）

Root `.env` (for docker-compose): `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DASHSCOPE_API_KEY`, `VITE_API_BASE_URL`.

## Database Schema

**Database**: `image_search` · **Extension**: `vector` (pgvector 0.8+)

### `products` (primary key: `model_number` VARCHAR — NOT auto-increment id)

| Field | Type | Constraint |
|-------|------|------------|
| model_number | VARCHAR(100) | PRIMARY KEY |
| photographer_file | VARCHAR(255) | NOT NULL |
| alibaba_product_url | VARCHAR(500) | NOT NULL |
| category | VARCHAR(100) | NOT NULL |
| spec_cn_reference / spec_cn / spec_en | TEXT | NULL |
| product_size / package_size | VARCHAR(200) | NULL |
| price_1688, fob_price_tier1/2/3, intl_platform_price, competitor_price | NUMERIC(10,2) | NULL |
| ref_link_1/2/3, intl_platform_url(_1/_2) | VARCHAR(500) | NULL |
| created_at, updated_at | TIMESTAMP | AUTO |

### `product_images`

| Field | Type | Constraint |
|-------|------|------------|
| id | SERIAL | PRIMARY KEY |
| model_number | VARCHAR(100) | FK → products, CASCADE DELETE |
| image_path | VARCHAR(255) | UNIQUE, NOT NULL (web path) |
| vector | vector(1024) | NOT NULL |
| content_hash | VARCHAR(64) | UNIQUE（全库），源文件 SHA-256，用于精确去重 |
| original_path / oss_path | TEXT | NULL |
| image_order | INT | DEFAULT 0 |
| is_primary | BOOLEAN | DEFAULT FALSE |
| created_at | TIMESTAMP | AUTO |

**Indexes**: `idx_product_images_model_number`, `uq_product_images_content_hash` (UNIQUE), `idx_product_images_vector_hnsw` (HNSW, `vector_cosine_ops`, m=16, ef_construction=64)

Schema is defined twice — keep both in sync when changing models:
1. [backend/models/product.py](backend/models/product.py) (SQLAlchemy, authoritative)
2. [postgres/init/01_init.sql](postgres/init/01_init.sql) (Docker first-boot init)

## Vector Search Implementation

**Core Class**: `VectorSearchService` ([backend/services/vector_search.py](backend/services/vector_search.py)) — stateless, no startup loading. `product_search.ImageSearchService` is a compat alias for the same class.

```python
search_similar_images(image_path, top_k=10, request_id=None) -> list   # embed + search_by_vector
search_by_vector(vector, top_k=10, request_id=None) -> list            # SQL 内过采样 + 折叠
```

**SQL Query Pattern**（cosine；先过采样再按型号折叠，不是取 top_k 张图后在 Python 里折叠）:
```sql
SET LOCAL hnsw.ef_search = :ef;   -- ef = max(fetch_n, 40)；LOCAL 避免污染连接池
WITH candidates AS MATERIALIZED (
    SELECT model_number, image_path, original_path, oss_path,
           vector <=> CAST(:q AS vector) AS distance
    FROM product_images ORDER BY vector <=> CAST(:q AS vector) LIMIT :fetch_n
), best AS (
    SELECT DISTINCT ON (model_number) * FROM candidates ORDER BY model_number, distance
)
SELECT * FROM best ORDER BY distance LIMIT :top_k;
```
- `fetch_n = clamp(top_k × SEARCH_OVERSAMPLE, top_k, 500)`，`SEARCH_OVERSAMPLE` 默认 5
- `similarity = min(1.0, max(0.0, 1.0 - distance))` —— 实测向量 L2 范数 1.000282，必须夹上界
- ⚠️ pgvector **不会**把 `ef_search` clamp 到 k（见 `src/hnswscan.c`），所以必须显式设置
- CTE 用 `MATERIALIZED` 强制两阶段执行（先 HNSW 取候选，再折叠），否则 PostgreSQL 12+ 可能内联 CTE 改变执行形状

## CSV Import

`POST /api/products/import-csv` (multipart field `csv_file`; UTF-8 / GBK / GB2312 / UTF-8-SIG auto-detected).

**Required columns**: `model_number`, `photographer_file`, `alibaba_product_url`, `category`
**Optional columns**: spec/price/link fields matching `products` columns. Rows with existing `model_number` are skipped.
CSV import creates product rows only — images and vectors are added via the product image upload flow (create/update product endpoints call `extract_feature` per image).

## 批量导入图片（CLI）

目录约定：`{root}/{model_number}/**/*.{jpg,png,…}` —— 一级子目录名即型号，目录内部递归收图。

```bash
cd backend
python -m scripts.ingest_images --root data/摄像师拍摄素材 --dry-run    # 先看报告
python -m scripts.ingest_images --root data/摄像师拍摄素材 --rebuild-index  # 首次全量
python -m scripts.ingest_images --root data/摄像师拍摄素材              # 日常增量
```

- **顺序**：先 `POST /api/products/import-csv` 建产品，再跑本脚本导图。目录名匹配不上已有 `model_number` 的会被跳过并在报告中列出（常见于 `CS-08` 应为 `CS-008` 这类命名错误）。
- **去重**：SHA-256 全库唯一。重复图片在调用 DashScope **之前**就被拦下，不花钱。
- **幂等**：重跑同一目录零 API 调用，因此断点续传无需 checkpoint。
- **性能**：实测批量 20 张/请求 = 89 ms/张（单张为 490 ms/张，约 5.5× 提升）。1000 张约 90 秒、¥0.2。
- `--rebuild-index` 会先 `DROP` 再重建 HNSW 索引（pgvector 官方建议先灌数据后建索引），仅首次全量导入使用。
- `--dry-run` 只扫描算哈希查重并报告，不调 API、不写库、不落盘；`--batch-size` clamp 到 `[1, 20]`；`--limit` 只处理前 N 张用于调试。

## Testing

```bash
cd backend
python -m pytest test/ -v                        # 全部（集成测试需要 docker compose up -d db）
python -m pytest test/integration/ -v            # 仅集成测试（真 PostgreSQL）
python -m pytest test/ --ignore=test/integration -v   # 仅单元测试（无需数据库）
python -m pytest test/ --cov=. --cov-report=html
```

集成测试连接 `DB_HOST:DB_PORT`（默认 `localhost:5433`）上的独立库 `image_search_test`，每个测试
重建表结构，不污染开发数据。PostgreSQL 不可达时整套集成测试自动 skip。

⚠️ `test/test_pgvector.py`、`test/benchmark_search.py` 是手工基准脚本，不是 pytest 用例。

## Important Architecture Notes

- ⚠️ **Primary key**: products use `model_number` (VARCHAR), all FKs and API URLs reference it.
- **File uploads**: max 16MB (Flask) / 20MB (nginx `client_max_body_size`); formats png/jpg/jpeg/gif/webp; stored under `backend/uploads/product_images/{model_number}/`.
- **Blueprints**: only `products_v2.py` is registered. `products.py`, `customers.py`, `orders.py`, `product_search.py` (blueprint), `oss.py` are legacy/unregistered.
- **批量导入**: 用 `python -m scripts.ingest_images`（见上方章节）。FAISS 时代的 `ingest_dataset.py` 已删除。
- **图片去重**: `product_images.content_hash` 为源文件 SHA-256，**全库唯一**。同一张图在任何型号下只能入库一次；重复上传返回 `{skipped: true, duplicate_of: ...}`。近似去重（pHash）暂未实现。
- **图片文件命名**: `uploads/product_images/{model_number}/{sha256前16位}{ext}`，不再用 UUID。同一张图永远落在同一路径，重复导入不产生新文件。
- **Legacy TS components** (`OrderManagement` etc.) have type errors but are excluded from the routed app; don't "fix" them unless reactivating.
- **Old container conflicts**: legacy containers named `fashion-crm-*` (MySQL-era / dockerhub images) may exist on dev machines; `docker rm` them if compose reports name conflicts. Legacy `mysql_data` volumes are orphaned and safe to ignore.
