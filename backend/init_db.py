from sqlalchemy import text

from app import create_app
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

        # 3. 为向量列建立 HNSW 索引（cosine 距离，与检索逻辑保持一致）
        db.session.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_product_images_vector_hnsw "
            "ON product_images USING hnsw (vector vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64)"
        ))
        db.session.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_image_assets_vector_active_hnsw "
            "ON image_assets USING hnsw (vector vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64) "
            "WHERE status = 'active'"
        ))
        db.session.commit()
        print("新旧图片表的 HNSW 向量索引创建成功！")

        # 4. 已有库的幂等收敛：create_all() 不会给已存在的表补列
        db.session.execute(text(
            'ALTER TABLE product_images ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)'
        ))
        db.session.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_product_images_content_hash '
            'ON product_images (content_hash)'
        ))
        db.session.commit()
        print("content_hash 列与唯一索引已就绪！")

        # 5. 旧表只做只读盘点，绝不在初始化期间自动删除或转换。
        legacy_image_count = db.session.execute(
            text('SELECT COUNT(*) FROM product_images')
        ).scalar_one()
        if legacy_image_count:
            print(
                '兼容迁移要求：product_images 中检测到 '
                f'{legacy_image_count} 条旧图片记录；'
                '已停止自动清理，请先制定并执行显式兼容迁移方案。'
            )
        else:
            print("product_images 当前为空，无需兼容迁移。")

if __name__ == '__main__':
    init_database()
