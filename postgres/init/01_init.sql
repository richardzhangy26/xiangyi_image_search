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

-- 3) image_assets 表（独立图片资产，不要求先关联商品）
CREATE TABLE IF NOT EXISTS image_assets (
    id                    UUID PRIMARY KEY,
    model_number          VARCHAR(100) REFERENCES products(model_number) ON DELETE SET NULL,
    source_provider       VARCHAR(32) NOT NULL,
    source_bucket         VARCHAR(255) NOT NULL,
    source_relative_path  TEXT NOT NULL,
    source_revision       INTEGER NOT NULL DEFAULT 1,
    display_name        TEXT NOT NULL,
    version             BIGINT NOT NULL DEFAULT 1,
    oss_path              TEXT NOT NULL UNIQUE,
    preview_oss_path      TEXT NOT NULL,
    content_hash          VARCHAR(64) NOT NULL,
    source_size           BIGINT NOT NULL,
    source_mime_type      VARCHAR(100) NOT NULL,
    source_width          INTEGER NOT NULL,
    source_height         INTEGER NOT NULL,
    vector                vector(1024) NOT NULL,
    embedding_model       VARCHAR(128) NOT NULL,
    embedding_dimension   SMALLINT NOT NULL,
    normalization_version VARCHAR(32) NOT NULL,
    status                VARCHAR(20) NOT NULL DEFAULT 'active',
    archived_at           TIMESTAMP,
    created_at            TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_image_assets_source_identity UNIQUE (
        source_provider,
        source_bucket,
        source_relative_path,
        source_revision
    ),
    CONSTRAINT ck_image_assets_status CHECK (status IN ('active', 'archived')),
    CONSTRAINT ck_image_assets_source_revision CHECK (source_revision >= 1),
    CONSTRAINT ck_image_assets_embedding_dimension CHECK (embedding_dimension = 1024),
    CONSTRAINT ck_image_assets_version CHECK (version >= 1)
);

COMMENT ON TABLE image_assets IS '独立图片资产：可无商品型号，源图与预览存放于私有 OSS';

-- 4) image_import_items 表（持久、可重启的异步 embedding 队列）
CREATE TABLE IF NOT EXISTS image_import_items (
    id                           UUID PRIMARY KEY,
    source_provider              VARCHAR(32) NOT NULL,
    source_bucket                VARCHAR(255) NOT NULL,
    source_relative_path         TEXT NOT NULL,
    source_revision              INTEGER NOT NULL DEFAULT 1,
    display_name                 TEXT NOT NULL,
    oss_path                     TEXT NOT NULL,
    preview_oss_path             TEXT NOT NULL,
    content_hash                 VARCHAR(64) NOT NULL,
    source_size                  BIGINT NOT NULL,
    source_mime_type             VARCHAR(100) NOT NULL,
    source_width                 INTEGER NOT NULL,
    source_height                INTEGER NOT NULL,
    normalization_version        VARCHAR(32) NOT NULL,
    expected_embedding_model     VARCHAR(128) NOT NULL
        DEFAULT 'tongyi-embedding-vision-plus-2026-03-06',
    expected_embedding_dimension SMALLINT NOT NULL DEFAULT 1024,
    status                       VARCHAR(20) NOT NULL DEFAULT 'queued',
    attempt_count                INTEGER NOT NULL DEFAULT 0,
    last_error_class             VARCHAR(32),
    last_attempt_at              TIMESTAMP,
    next_retry_at                TIMESTAMP,
    asset_id                     UUID REFERENCES image_assets(id) ON DELETE SET NULL,
    request_id                   VARCHAR(64) NOT NULL,
    claim_token                  UUID,
    claim_generation             BIGINT NOT NULL DEFAULT 0,
    claimed_by                   VARCHAR(128),
    claimed_at                   TIMESTAMP,
    lease_expires_at             TIMESTAMP,
    embedding_started_at         TIMESTAMP,
    completed_at                 TIMESTAMP,
    failed_at                    TIMESTAMP,
    failure_message              VARCHAR(512),
    cancel_requested_at          TIMESTAMP,
    cancel_requested_by          VARCHAR(128),
    cancelled_at                 TIMESTAMP,
    purge_eligible_at            TIMESTAMP,
    objects_purged_at            TIMESTAMP,
    created_at                   TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at                   TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_image_import_items_source_identity UNIQUE (
        source_provider,
        source_bucket,
        source_relative_path,
        source_revision
    ),
    CONSTRAINT ck_image_import_items_status_v2 CHECK (
        status IN ('queued', 'embedding', 'completed', 'failed', 'awaiting_retry', 'cancelled', 'abandoned')
    ),
    CONSTRAINT ck_image_import_items_attempt_count CHECK (attempt_count >= 0),
    CONSTRAINT ck_image_import_items_source_revision CHECK (source_revision >= 1),
    CONSTRAINT ck_image_import_items_embedding_model CHECK (
        expected_embedding_model = 'tongyi-embedding-vision-plus-2026-03-06'
    ),
    CONSTRAINT ck_image_import_items_embedding_dimension CHECK (
        expected_embedding_dimension = 1024
    ),
    CONSTRAINT ck_image_import_items_claim_generation CHECK (claim_generation >= 0)
);

CREATE OR REPLACE FUNCTION set_image_asset_display_name()
RETURNS trigger AS $$
BEGIN
    IF NEW.display_name IS NULL THEN
        NEW.display_name := regexp_replace(NEW.source_relative_path, '^.*/', '');
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_image_assets_display_name ON image_assets;
CREATE TRIGGER trg_image_assets_display_name
    BEFORE INSERT ON image_assets
    FOR EACH ROW EXECUTE FUNCTION set_image_asset_display_name();

CREATE TABLE IF NOT EXISTS asset_activity_records (
    id          UUID PRIMARY KEY,
    event_type  VARCHAR(64) NOT NULL,
    target_type VARCHAR(32) NOT NULL,
    target_id   TEXT NOT NULL,
    request_id  VARCHAR(64) NOT NULL,
    source      VARCHAR(32) NOT NULL,
    actor_id    TEXT,
    batch_id    TEXT,
    task_id     TEXT,
    before_state JSONB,
    after_state  JSONB,
    result       VARCHAR(32) NOT NULL,
    error_code   VARCHAR(64),
    created_at   TIMESTAMP NOT NULL DEFAULT NOW()
);

-- 5) 索引
CREATE INDEX IF NOT EXISTS idx_image_assets_content_hash
    ON image_assets (content_hash);

CREATE INDEX IF NOT EXISTS idx_image_assets_model_number
    ON image_assets (model_number);

CREATE INDEX IF NOT EXISTS idx_image_assets_status
    ON image_assets (status);

CREATE INDEX IF NOT EXISTS idx_asset_activity_target_created
    ON asset_activity_records (target_type, target_id, created_at);

CREATE INDEX IF NOT EXISTS idx_asset_activity_request_id
    ON asset_activity_records (request_id);

CREATE INDEX IF NOT EXISTS idx_image_import_items_claim_order
    ON image_import_items (status, created_at, id);

CREATE INDEX IF NOT EXISTS idx_image_import_items_lease
    ON image_import_items (status, lease_expires_at);

CREATE INDEX IF NOT EXISTS idx_image_import_items_retry_schedule
    ON image_import_items (status, next_retry_at);

CREATE INDEX IF NOT EXISTS idx_image_import_items_purge_schedule
    ON image_import_items (status, purge_eligible_at);

CREATE INDEX IF NOT EXISTS idx_image_assets_vector_active_hnsw
    ON image_assets
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE status = 'active';

-- 6) 帮助优化器选用索引
ANALYZE products;
ANALYZE image_assets;
ANALYZE image_import_items;
