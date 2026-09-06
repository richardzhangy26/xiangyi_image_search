# Backend Architecture (Current)

## Online Image Search Path

- Active API path: `products_v2` blueprint in `backend/blueprints/products_v2.py`
- Search endpoint: `POST /api/products/search`
- Product endpoint: `POST /api/products`
- Search engine: `VectorSearchService` in `backend/services/vector_search.py`; `ImageSearchService`
  in `backend/product_search.py` is a compat alias (`ImageSearchService = VectorSearchService`) kept
  only so `app.py` and older tests don't need import changes
- Query images are normalized through the shared `ImageNormalizer`, embedded from the resulting
  temporary JPEG, and removed after the request; they are never written to OSS or PostgreSQL
- Vector storage for online search: PostgreSQL `pgvector` column `image_assets.vector`

## Search Behavior Contracts

- `top_k` must be in range `1..50`, default `10`
- Search returns active image assets as image-level Top-K results; each asset occupies one result
  position, even when several assets share a `model_number` or `content_hash`
- `model_number` is optional; unassigned assets remain valid results and are identified by their
  complete source-relative path
- Query-image decoding/normalization failures return `4xx`, including an explicit `413` for an
  oversized request
- Embedding failures return `5xx` (no silent downgrade to empty result)
- Vector query failures return `5xx`

## Vector Similarity + Indexing

- Similarity metric: cosine distance (`vector <=> CAST(:q AS vector)` in raw SQL, not the SQLAlchemy
  `.cosine_distance()` ORM helper)
- Similarity score returned to caller: `min(1.0, max(0.0, 1.0 - distance))` — clamped on **both**
  ends. The lower-bound clamp alone is not enough: measured query vectors have L2 norm 1.000282
  (not exactly 1.0), so a self-match can compute a cosine similarity of ~1.00056; without the upper
  clamp this renders as "100.1% similar" in the UI
- Search filters to `image_assets.status = 'active'`, orders directly by cosine distance, and uses
  transaction-local `hnsw.ef_search = max(top_k, 40)` without product joins or model-level folding
- Schema/index setup: `postgres/init/01_init.sql` (Docker first-boot init) and `backend/init_db.py`
  (idempotent one-shot init script) — both must stay in sync with the SQLAlchemy models
  - `CREATE EXTENSION IF NOT EXISTS vector`
  - Partial HNSW index on active `image_assets.vector` with `vector_cosine_ops`
    (`m=16, ef_construction=64`)

## Deprecated Paths

- Legacy FAISS / mixed schema workflows are deprecated for online path
- `backend/scripts/ingest_images.py` 仅保留 `--dry-run` 只读盘点，写入
  `product_images` 的模式已停用；Kodo 正式迁移使用 `scripts.migrate_kodo_to_oss`
