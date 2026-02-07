# Backend Architecture (Current)

## Online Image Search Path

- Active API path: `products_v2` blueprint in `backend/blueprints/products_v2.py`
- Search endpoint: `POST /api/products/search`
- Product endpoint: `POST /api/products`
- Search engine: `ImageSearchService` in `backend/product_search.py`
- Vector storage: PostgreSQL `pgvector` column `product_images.vector`

## Search Behavior Contracts

- `top_k` must be in range `1..50`, default `10`
- Search is model-level deduped by default (`model_number` unique in response)
- Embedding failures return `5xx` (no silent downgrade to empty result)
- Vector query failures return `5xx`

## Vector Similarity + Indexing

- Similarity metric: cosine distance (`cosine_distance`)
- Similarity score returned to caller: `1 - cosine_distance` (clamped at `>= 0`)
- Required DB migration: `backend/supabase/migrations/004_pgvector_hnsw_index.sql`
  - `CREATE EXTENSION IF NOT EXISTS vector`
  - HNSW index on `product_images.vector` with `vector_cosine_ops`

## Deprecated Paths

- Legacy FAISS / mixed schema workflows are deprecated for online path
- `backend/scripts/ingest_dataset.py` is disabled to avoid accidental usage
