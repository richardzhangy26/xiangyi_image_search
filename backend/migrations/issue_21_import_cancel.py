"""Issue #21 显式、幂等、只扩展的 PostgreSQL 迁移。

为 image_import_items 增加取消意图与取消终态：新增可空列，状态约束替换为
包含旧状态的超集（旧行始终有效）。不删除数据、不收缩任何既有能力。

本模块不会由应用启动、健康检查或 worker 隐式导入。生产执行必须显式传入
``--apply``。
"""

import argparse

from sqlalchemy import text


MIGRATION_STATEMENTS = (
    """
    ALTER TABLE image_import_items
        ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMP
    """,
    """
    ALTER TABLE image_import_items
        ADD COLUMN IF NOT EXISTS cancel_requested_by VARCHAR(128)
    """,
    """
    ALTER TABLE image_import_items
        ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP
    """,
    """
    DO $$
    BEGIN
        -- Issue #22 汇合：Issue #20 可能已先创建含 awaiting_retry 的五状态
        -- ck_image_import_items_status_v2；此处删除后重建为六状态超集，
        -- 保证 cancelled 与 awaiting_retry 同时有效（重复执行结果一致）。
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
                'awaiting_retry', 'cancelled'
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
