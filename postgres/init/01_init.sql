-- ========================================
-- 商品图像搜索系统 PostgreSQL 初始化脚本
-- 由 docker-entrypoint-initdb.d 在数据库首次启动时自动执行
-- 也可手动执行: psql -U postgres -d image_search -f 01_init.sql
-- 表结构与 backend/models/product.py 保持一致
-- ========================================

-- 1) 启用 pgvector 扩展（向量搜索）
CREATE EXTENSION IF NOT EXISTS vector;

-- 2) products 表（电子产品配件主表）
CREATE TABLE IF NOT EXISTS products (
    -- 主键和必填字段
    model_number        VARCHAR(100) PRIMARY KEY,
    photographer_file   VARCHAR(255) NOT NULL,
    alibaba_product_url VARCHAR(500) NOT NULL,
    category            VARCHAR(100) NOT NULL,

    -- 产品参数
    spec_cn_reference   TEXT,
    spec_cn             TEXT,
    spec_en             TEXT,
    product_size        VARCHAR(200),
    package_size        VARCHAR(200),

    -- 价格相关（单位：美元）
    price_1688          NUMERIC(10, 2),
    fob_price_tier1     NUMERIC(10, 2),
    fob_price_tier2     NUMERIC(10, 2),
    fob_price_tier3     NUMERIC(10, 2),
    intl_platform_price NUMERIC(10, 2),
    competitor_price    NUMERIC(10, 2),

    -- 参考链接
    ref_link_1          VARCHAR(500),
    ref_link_2          VARCHAR(500),
    ref_link_3          VARCHAR(500),
    intl_platform_url   VARCHAR(500),
    intl_platform_url_1 VARCHAR(500),
    intl_platform_url_2 VARCHAR(500),

    -- 系统字段
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW()
);

COMMENT ON TABLE products IS '电子产品配件主表（相机肩带、挂绳等）';

-- 3) product_images 表（产品图片 + 1024 维向量）
CREATE TABLE IF NOT EXISTS product_images (
    id            SERIAL PRIMARY KEY,
    model_number  VARCHAR(100) NOT NULL REFERENCES products(model_number) ON DELETE CASCADE,
    image_path    VARCHAR(255) NOT NULL UNIQUE,
    vector        vector(1024) NOT NULL,
    original_path TEXT,
    oss_path      TEXT,
    image_order   INTEGER DEFAULT 0,
    is_primary    BOOLEAN DEFAULT FALSE,
    created_at    TIMESTAMP DEFAULT NOW()
);

COMMENT ON TABLE product_images IS '产品图片表，存储图片路径和 DashScope 1024 维图像向量';

-- 4) 索引
-- 外键查询索引
CREATE INDEX IF NOT EXISTS idx_product_images_model_number
    ON product_images (model_number);

-- HNSW 向量索引（cosine 距离，embedding 为归一化向量，检索统一使用 cosine_distance）
CREATE INDEX IF NOT EXISTS idx_product_images_vector_hnsw
    ON product_images
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- 5) 帮助优化器选用索引
ANALYZE products;
ANALYZE product_images;
