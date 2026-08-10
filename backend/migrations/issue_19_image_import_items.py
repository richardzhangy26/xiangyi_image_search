"""Issue #19 显式、幂等、只扩展的 PostgreSQL 迁移。

本模块不会由应用启动、健康检查或 worker 隐式导入。生产执行必须显式传入
``--apply``。
"""

import argparse

from sqlalchemy import text


MIGRATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS image_import_items (
        id UUID PRIMARY KEY,
        source_provider VARCHAR(32) NOT NULL,
        source_bucket VARCHAR(255) NOT NULL,
        source_relative_path TEXT NOT NULL,
        source_revision INTEGER NOT NULL DEFAULT 1,
        display_name TEXT NOT NULL,
        oss_path TEXT NOT NULL,
        preview_oss_path TEXT NOT NULL,
        content_hash VARCHAR(64) NOT NULL,
        source_size BIGINT NOT NULL,
        source_mime_type VARCHAR(100) NOT NULL,
        source_width INTEGER NOT NULL,
        source_height INTEGER NOT NULL,
        normalization_version VARCHAR(32) NOT NULL,
        expected_embedding_model VARCHAR(128) NOT NULL
            DEFAULT 'tongyi-embedding-vision-plus-2026-03-06',
        expected_embedding_dimension SMALLINT NOT NULL DEFAULT 1024,
        status VARCHAR(20) NOT NULL DEFAULT 'queued',
        asset_id UUID REFERENCES image_assets(id) ON DELETE SET NULL,
        request_id VARCHAR(64) NOT NULL,
        claim_token UUID,
        claim_generation BIGINT NOT NULL DEFAULT 0,
        claimed_by VARCHAR(128),
        claimed_at TIMESTAMP,
        lease_expires_at TIMESTAMP,
        embedding_started_at TIMESTAMP,
        completed_at TIMESTAMP,
        failed_at TIMESTAMP,
        failure_message VARCHAR(512),
        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_image_import_items_source_identity UNIQUE (
            source_provider,
            source_bucket,
            source_relative_path,
            source_revision
        ),
        CONSTRAINT ck_image_import_items_status CHECK (
            status IN ('queued', 'embedding', 'completed', 'failed')
        ),
        CONSTRAINT ck_image_import_items_source_revision CHECK (
            source_revision >= 1
        ),
        CONSTRAINT ck_image_import_items_embedding_model CHECK (
            expected_embedding_model = 'tongyi-embedding-vision-plus-2026-03-06'
        ),
        CONSTRAINT ck_image_import_items_embedding_dimension CHECK (
            expected_embedding_dimension = 1024
        ),
        CONSTRAINT ck_image_import_items_claim_generation CHECK (
            claim_generation >= 0
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_image_import_items_claim_order
    ON image_import_items (status, created_at, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_image_import_items_lease
    ON image_import_items (status, lease_expires_at)
    """,
)


def apply_migration(connection):
    """在调用方管理的 PostgreSQL 事务中执行迁移。"""
    for statement in MIGRATION_STATEMENTS:
        connection.execute(text(statement))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        parser.error('必须显式传入 --apply；迁移未执行')

    from app import create_app
    from models import db

    app = create_app()
    with app.app_context(), db.engine.begin() as connection:
        apply_migration(connection)


if __name__ == '__main__':
    main()
