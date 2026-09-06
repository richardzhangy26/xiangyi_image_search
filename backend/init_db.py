from sqlalchemy import text

from app import create_app
from migrations.issue_15_asset_display_name import (
    DISPLAY_NAME_TRIGGER_STATEMENTS,
)
from models import db


def init_database():
    app = create_app()
    with app.app_context():
        # 1. 启用 pgvector 扩展（Vector 列类型依赖该扩展）
        db.session.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        db.session.commit()
        print("pgvector 扩展已启用！")

        # 2. 创建所有表
        db.create_all()
        print("数据库表创建成功！")

        # 3. 安装旧应用写入兼容触发器；此脚本本身仍须由操作员显式执行。
        for statement in DISPLAY_NAME_TRIGGER_STATEMENTS:
            db.session.execute(text(statement))

        # 4. 为活动图片资产向量列建立 HNSW 索引（cosine 距离）
        db.session.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_image_assets_vector_active_hnsw "
            "ON image_assets USING hnsw (vector vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64) "
            "WHERE status = 'active'"
        ))
        db.session.commit()
        print("图片资产兼容触发器与 HNSW 向量索引创建成功！")

if __name__ == '__main__':
    init_database()
