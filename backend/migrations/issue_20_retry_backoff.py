"""Issue #20 显式、幂等、只扩展的 PostgreSQL 迁移。

为 image_import_items 增加重试调度字段与 awaiting_retry 状态：
新增可空/带默认值列，状态约束替换为包含旧状态的超集（旧行始终有效），
不删除数据、不收缩任何既有能力。

本模块不会由应用启动、健康检查或 worker 隐式导入。生产执行必须显式传入
``--apply``。
"""

import argparse

from sqlalchemy import text


MIGRATION_STATEMENTS = (
    """
    ALTER TABLE image_import_items
        ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE image_import_items
        ADD COLUMN IF NOT EXISTS last_error_class VARCHAR(32)
    """,
    """
    ALTER TABLE image_import_items
        ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMP
    """,
    """
    ALTER TABLE image_import_items
        ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMP
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_image_import_items_attempt_count'
        ) THEN
            ALTER TABLE image_import_items
                ADD CONSTRAINT ck_image_import_items_attempt_count
                CHECK (attempt_count >= 0);
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_image_import_items_status_v2'
        ) THEN
            ALTER TABLE image_import_items
                ADD CONSTRAINT ck_image_import_items_status_v2
                CHECK (status IN (
                    'queued', 'embedding', 'completed', 'failed',
                    'awaiting_retry'
                ));
        END IF;

        IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_image_import_items_status'
        ) THEN
            ALTER TABLE image_import_items
                DROP CONSTRAINT ck_image_import_items_status;
        END IF;
    END $$;
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_image_import_items_retry_schedule
    ON image_import_items (status, next_retry_at)
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
