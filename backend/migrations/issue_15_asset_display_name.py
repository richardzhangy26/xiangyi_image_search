"""Issue #15 explicit, idempotent PostgreSQL migration.

This module is intentionally disconnected from app startup and health checks.
Production execution requires the explicit ``--apply`` flag.
"""

import argparse

from sqlalchemy import text


DISPLAY_NAME_TRIGGER_STATEMENTS = (
    """
    CREATE OR REPLACE FUNCTION set_image_asset_display_name()
    RETURNS trigger AS $$
    BEGIN
        IF NEW.display_name IS NULL THEN
            NEW.display_name := regexp_replace(
                NEW.source_relative_path, '^.*/', ''
            );
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    'DROP TRIGGER IF EXISTS trg_image_assets_display_name ON image_assets',
    """
    CREATE TRIGGER trg_image_assets_display_name
    BEFORE INSERT ON image_assets
    FOR EACH ROW EXECUTE FUNCTION set_image_asset_display_name()
    """,
)


MIGRATION_STATEMENTS = (
    'ALTER TABLE image_assets ADD COLUMN IF NOT EXISTS display_name TEXT',
    'ALTER TABLE image_assets ADD COLUMN IF NOT EXISTS version BIGINT',
    """
    UPDATE image_assets
    SET display_name = regexp_replace(source_relative_path, '^.*/', '')
    WHERE display_name IS NULL
    """,
    'UPDATE image_assets SET version = 1 WHERE version IS NULL',
    *DISPLAY_NAME_TRIGGER_STATEMENTS,
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM image_assets
            WHERE display_name IS NULL OR display_name = '' OR version IS NULL
        ) THEN
            RAISE EXCEPTION 'image_assets display name backfill is incomplete';
        END IF;
    END $$
    """,
    'ALTER TABLE image_assets ALTER COLUMN display_name SET NOT NULL',
    'ALTER TABLE image_assets ALTER COLUMN version SET DEFAULT 1',
    'ALTER TABLE image_assets ALTER COLUMN version SET NOT NULL',
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_image_assets_version'
              AND conrelid = 'image_assets'::regclass
        ) THEN
            ALTER TABLE image_assets
            ADD CONSTRAINT ck_image_assets_version CHECK (version >= 1);
        END IF;
    END $$
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_activity_records (
        id UUID PRIMARY KEY,
        event_type VARCHAR(64) NOT NULL,
        target_type VARCHAR(32) NOT NULL,
        target_id TEXT NOT NULL,
        request_id VARCHAR(64) NOT NULL,
        source VARCHAR(32) NOT NULL,
        actor_id TEXT,
        batch_id TEXT,
        task_id TEXT,
        before_state JSONB,
        after_state JSONB,
        result VARCHAR(32) NOT NULL,
        error_code VARCHAR(64),
        created_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_asset_activity_target_created
    ON asset_activity_records (target_type, target_id, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_asset_activity_request_id
    ON asset_activity_records (request_id)
    """,
)


def apply_migration(connection):
    """Apply all statements on a caller-managed PostgreSQL connection."""
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
