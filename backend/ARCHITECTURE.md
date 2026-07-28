# Backend Architecture (Current)

## Online Image Search Path

- Active API path: `products_v2` blueprint in `backend/blueprints/products_v2.py`
- Search endpoint: `POST /api/products/search`
- Product endpoint: `POST /api/products`
- Search engine: `VectorSearchService` in `backend/services/vector_search.py`; `ImageSearchService`
  in `backend/product_search.py` is a compat alias (`ImageSearchService = VectorSearchService`) kept
  only so `app.py` and older tests don't need import changes
- Vector storage: PostgreSQL `pgvector` column `product_images.vector`

## Search Behavior Contracts

- `top_k` must be in range `1..50`, default `10`
- Search is model-level deduped by default (`model_number` unique in response) — folding happens
  inside the SQL query itself (`DISTINCT ON (model_number)` over an oversampled candidate set), not
  by taking `top_k` rows and deduping in Python
- Embedding failures return `5xx` (no silent downgrade to empty result)
- Vector query failures return `5xx`

## Vector Similarity + Indexing

- Similarity metric: cosine distance (`vector <=> CAST(:q AS vector)` in raw SQL, not the SQLAlchemy
  `.cosine_distance()` ORM helper)
- Similarity score returned to caller: `min(1.0, max(0.0, 1.0 - distance))` — clamped on **both**
  ends. The lower-bound clamp alone is not enough: measured query vectors have L2 norm 1.000282
  (not exactly 1.0), so a self-match can compute a cosine similarity of ~1.00056; without the upper
  clamp this renders as "100.1% similar" in the UI
- Schema/index setup: `postgres/init/01_init.sql` (Docker first-boot init) and `backend/init_db.py`
  (idempotent one-shot init script) — both must stay in sync with `backend/models/product.py`
  - `CREATE EXTENSION IF NOT EXISTS vector`
  - HNSW index on `product_images.vector` with `vector_cosine_ops` (`m=16, ef_construction=64`)

## Deprecated Paths

- Legacy FAISS / mixed schema workflows are deprecated for online path
- `backend/scripts/ingest_dataset.py`（FAISS 时代）已删除；批量导入改用
  `python -m scripts.ingest_images --root <素材目录>`（见 `backend/scripts/ingest_images.py`）
