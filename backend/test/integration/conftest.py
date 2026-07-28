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


@pytest.fixture(scope='session')
def _test_database():
    """确保测试库存在；PostgreSQL 不可用时跳过整个集成测试套件。"""
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
    except Exception as exc:  # noqa: BLE001 - 任何连接失败都应跳过而非报错
        pytest.skip(f'PostgreSQL 不可用（{exc}），跳过集成测试')
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
        yield application
        db.session.remove()
