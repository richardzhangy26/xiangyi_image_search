"""集成测试夹具：连接本机 5433 上的 Docker PostgreSQL，使用独立测试库。

必须在任何 `import app` 之前设置 DATABASE_URL —— app.py 在模块导入时读取该变量，
且 Flask-SQLAlchemy 3.1 在 init_app() 阶段就创建了 engine。
"""
import os
import sys
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy import text

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

TEST_DB_NAME = 'image_search_test'


def _dsn(database):
    user = os.getenv('DB_USER', 'postgres')
    password = os.getenv('DB_PASSWORD', '')
    host = os.getenv('DB_HOST', 'localhost')
    port = os.getenv('DB_PORT', '5433')
    return f'postgresql://{user}:{password}@{host}:{port}/{database}'


# 关键：必须在 import app 之前生效
os.environ['DATABASE_URL'] = _dsn(TEST_DB_NAME)

# 仅当 PostgreSQL 服务器物理不可达时才跳过整个集成测试套件；
# 认证失败、权限不足等配置错误必须让测试真实报错，不能被吞成"跳过"。
#
# 注意：本机 "localhost" 同时解析到 ::1 和 127.0.0.1，而 Docker 只监听 127.0.0.1，
# 所以即便密码错了，libpq 的错误文本里也会先带一行 IPv6 的 "Connection refused"
# （来自必然失败的 ::1 尝试），因此必须优先判定"认证/权限"特征命中，
# 命中则视为真实错误，即使消息里同时出现连接层面的关键词也不能跳过。
_AUTH_OR_PERMISSION_PATTERNS = (
    'password authentication failed',
    'authentication failed',
    'permission denied',
    'no password supplied',  # DB_PASSWORD 未设置/为空时 libpq 报的是这个，不是 "authentication failed"
)
_CONNECTION_ERROR_PATTERNS = (
    'connection refused',
    'could not connect',
    'timeout',
    'could not translate host name',
)


@pytest.fixture(scope='session')
def _test_database():
    """确保测试库存在；仅服务器不可达时跳过，其余错误一律真实失败。"""
    try:
        engine = sqlalchemy.create_engine(_dsn('postgres'), isolation_level='AUTOCOMMIT')
        with engine.connect() as conn:
            exists = conn.execute(
                text('SELECT 1 FROM pg_database WHERE datname = :name'),
                {'name': TEST_DB_NAME},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE {TEST_DB_NAME}'))
        engine.dispose()
    except sqlalchemy.exc.OperationalError as exc:
        message = str(exc).lower()
        is_auth_or_permission_error = any(p in message for p in _AUTH_OR_PERMISSION_PATTERNS)
        is_connection_error = any(p in message for p in _CONNECTION_ERROR_PATTERNS)
        if is_connection_error and not is_auth_or_permission_error:
            pytest.skip(f'PostgreSQL 服务器不可达（{exc}），跳过集成测试')
        raise
    return _dsn(TEST_DB_NAME)


@pytest.fixture()
def app(_test_database, tmp_path):
    """每个测试一套干净的表结构。"""
    from app import create_app
    from models import db

    application = create_app()
    application.config['TESTING'] = True
    application.config['UPLOAD_FOLDER'] = str(tmp_path / 'uploads')
    os.makedirs(application.config['UPLOAD_FOLDER'], exist_ok=True)

    with application.app_context():
        db.session.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        db.session.commit()
        db.drop_all()
        db.create_all()
        # create_all() 不会建这个索引——models/product.py 没有用 SQLAlchemy Index
        # 声明它，HNSW 索引只由 postgres/init/01_init.sql / backend/init_db.py 的
        # 原生 SQL 创建。不补建的话，涉及 hnsw.ef_search 的检索测试会退化成
        # Seq Scan 精确扫描，测不出近似最近邻检索的真实行为（见 T3 fix round 1）。
        # 索引名与参数须与 postgres/init/01_init.sql、backend/init_db.py 保持一致。
        db.session.execute(text(
            'CREATE INDEX IF NOT EXISTS idx_product_images_vector_hnsw '
            'ON product_images USING hnsw (vector vector_cosine_ops) '
            'WITH (m = 16, ef_construction = 64)'
        ))
        db.session.commit()
        yield application
        db.session.remove()
