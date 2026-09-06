"""Issue #19 显式迁移验收场景。

安全说明：这些测试要求本地隔离 PostgreSQL schema；本 Ticket 未获执行授权，
因此当前验收不得收集或运行本文件。
"""

from sqlalchemy import inspect, text

from migrations.issue_19_image_import_items import apply_migration
from models import db


def test_image_import_migration_is_idempotent_and_preserves_existing_rows(app):
    connection = db.session.connection()
    connection.execute(text('DROP TABLE image_import_items'))

    apply_migration(connection)
    connection.execute(text("""
        INSERT INTO image_import_items (
            id, source_provider, source_bucket, source_relative_path,
            source_revision, display_name, oss_path, preview_oss_path,
            content_hash, source_size, source_mime_type, source_width,
            source_height, normalization_version, expected_embedding_model,
            expected_embedding_dimension, status, request_id
        ) VALUES (
            gen_random_uuid(), 'image-import-upload', 'image-imports',
            'imports/hash/0001/item.png', 1, 'item.png',
            'private/original', 'private/preview', repeat('a', 64), 10,
            'image/png', 2, 2, 'preview-v1',
            'tongyi-embedding-vision-plus-2026-03-06', 1024,
            'queued', 'migration-19'
        )
    """))

    apply_migration(connection)

    assert connection.execute(text(
        "SELECT status FROM image_import_items WHERE request_id = 'migration-19'"
    )).scalar_one() == 'queued'
    schema_name = connection.execute(text('SELECT current_schema()')).scalar_one()
    indexes = {
        item['name']
        for item in inspect(connection).get_indexes(
            'image_import_items', schema=schema_name
        )
    }
    assert {
        'idx_image_import_items_claim_order',
        'idx_image_import_items_lease',
    } <= indexes

