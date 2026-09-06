"""Issue #22 显式、幂等、只扩展的 PostgreSQL 迁移。

为 image_import_items 增加保留期与清理字段，并把状态约束重建为包含
abandoned 的七状态超集。不删除数据、不收缩任何既有能力。

本模块不会由应用启动、健康检查或 worker 隐式导入。生产执行必须显式传入
``--apply``。
"""

import argparse

from sqlalchemy import text


MIGRATION_STATEMENTS = (
    """
    ALTER TABLE image_import_items
        ADD COLUMN IF NOT EXISTS purge_eligible_at TIMESTAMP
    """,
    """
    ALTER TABLE image_import_items
        ADD COLUMN IF NOT EXISTS objects_purged_at TIMESTAMP
    """,
    """
    DO $$
    BEGIN
        -- 删除 Issue #20/#21 汇合后的六状态 v2 约束，重建为七状态超集。
        IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_image_import_items_status_v2'
        ) THEN
            ALTER TABLE image_import_items
                DROP CONSTRAINT ck_image_import_items_status_v2;
        END IF;

        ALTER TABLE image_import_items
            ADD CONSTRAINT ck_image_import_items_status_v2
            CHECK (status IN (
                'queued', 'embedding', 'completed', 'failed',
                'awaiting_retry', 'cancelled', 'abandoned'
            ));

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
    CREATE INDEX IF NOT EXISTS idx_image_import_items_purge_schedule
    ON image_import_items (status, purge_eligible_at)
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
