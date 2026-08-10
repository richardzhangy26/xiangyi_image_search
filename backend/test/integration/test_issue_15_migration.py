"""Issue #15 migration acceptance scenarios.

Safety note: these tests intentionally require an isolated PostgreSQL schema.
They must not be executed without explicit user authorization.
"""

from sqlalchemy import text

from migrations.issue_15_asset_display_name import apply_migration
from models import db


def test_migration_backfills_basename_and_is_idempotent(app):
    connection = db.session.connection()
    connection.execute(text(
        'ALTER TABLE image_assets DROP COLUMN display_name'
    ))
    connection.execute(text(
        'ALTER TABLE image_assets DROP COLUMN version'
    ))
    connection.execute(text('DROP TABLE asset_activity_records'))
    connection.execute(text(
        """
        INSERT INTO image_assets (
            id, model_number, source_provider, source_bucket,
            source_relative_path, source_revision, oss_path, preview_oss_path,
            content_hash, source_size, source_mime_type, source_width,
            source_height, vector, embedding_model, embedding_dimension,
            normalization_version, status, created_at, updated_at
        ) VALUES (
            gen_random_uuid(), NULL, 'qiniu-kodo', 'bucket',
            '中文 目录/旧名称.PNG', 1, 'object/original', 'object/preview',
            repeat('a', 64), 1, 'image/png', 1, 1,
            array_fill(0.0, ARRAY[1024])::vector,
            'tongyi-embedding-vision-plus-2026-03-06', 1024,
            'preview-v1', 'active', NOW(), NOW()
        )
        """
    ))

    apply_migration(connection)
    backfilled = connection.execute(text(
        'SELECT display_name, version FROM image_assets'
    )).one()
    assert backfilled.display_name == '旧名称.PNG'
    assert backfilled.version == 1

    connection.execute(text(
        "UPDATE image_assets SET display_name = '人工名称.PNG', version = 7"
    ))
    apply_migration(connection)
    preserved = connection.execute(text(
        'SELECT display_name, version FROM image_assets'
    )).one()
    assert preserved.display_name == '人工名称.PNG'
    assert preserved.version == 7


def test_migrated_schema_accepts_legacy_insert_without_new_columns(app):
    connection = db.session.connection()
    apply_migration(connection)

    row = connection.execute(text(
        """
        INSERT INTO image_assets (
            id, model_number, source_provider, source_bucket,
            source_relative_path, source_revision, oss_path, preview_oss_path,
            content_hash, source_size, source_mime_type, source_width,
            source_height, vector, embedding_model, embedding_dimension,
            normalization_version, status, created_at, updated_at
        ) VALUES (
            gen_random_uuid(), NULL, 'legacy', 'bucket', '目录/兼容写入.webp',
            1, 'legacy/original', 'legacy/preview', repeat('b', 64), 1,
            'image/webp', 1, 1, array_fill(0.0, ARRAY[1024])::vector,
            'tongyi-embedding-vision-plus-2026-03-06', 1024,
            'preview-v1', 'active', NOW(), NOW()
        ) RETURNING display_name, version
        """
    )).one()

    assert row.display_name == '兼容写入.webp'
    assert row.version == 1
