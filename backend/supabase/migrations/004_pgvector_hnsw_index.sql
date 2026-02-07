-- pgvector extension + HNSW index for image search
-- 目标：为 product_images.vector 建立 cosine 距离索引，避免数据增大后全表扫描

-- 1) 确保 pgvector 可用
CREATE EXTENSION IF NOT EXISTS vector;

-- 2) 为向量列建立 HNSW 索引（使用 cosine opclass）
-- 说明：当前 embedding 为归一化向量，检索统一使用 cosine_distance
CREATE INDEX IF NOT EXISTS idx_product_images_vector_hnsw
    ON public.product_images
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- 3) 可选：运行 ANALYZE，帮助优化器更快选用索引
ANALYZE public.product_images;
