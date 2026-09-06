"""为 image_assets 增加 sort_order 列并按 created_at 回填存量数据。

只回填 active 资产（0 起连续，与读路径排序一致）；archived 资产保持默认
0，不影响任何读路径。幂等：列已存在时直接跳过，不会覆盖用户已调整过的
顺序。
运行方式（backend 目录）：python -m scripts.migrate_image_asset_sort_order
"""

from sqlalchemy import text

from app import create_app
from models import db

_COLUMN_CHECK_SQL = text(
    "SELECT 1 FROM information_schema.columns "
    "WHERE table_schema = 'public' "
    "AND table_name = 'image_assets' AND column_name = 'sort_order'"
)

_ADD_COLUMN_SQL = text(
    "ALTER TABLE image_assets "
    "ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
)

_BACKFILL_SQL = text(
    """
    WITH ranked AS (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY model_number
                   ORDER BY created_at, id
               ) - 1 AS new_order
        FROM image_assets
        WHERE model_number IS NOT NULL AND status = 'active'
    )
    UPDATE image_assets
    SET sort_order = ranked.new_order
    FROM ranked
    WHERE image_assets.id = ranked.id
    """
)


def migrate_image_asset_sort_order():
    """返回 True 表示执行了迁移，False 表示列已存在无需处理。"""
    with db.session.begin():
        if db.session.execute(_COLUMN_CHECK_SQL).scalar():
            return False
        db.session.execute(_ADD_COLUMN_SQL)
        db.session.execute(_BACKFILL_SQL)
    return True


def main():
    app = create_app()
    with app.app_context():
        if migrate_image_asset_sort_order():
            print('已添加 image_assets.sort_order 并按创建时间回填存量顺序。')
        else:
            print('image_assets.sort_order 已存在，跳过迁移（未改动任何数据）。')


if __name__ == '__main__':
    main()
