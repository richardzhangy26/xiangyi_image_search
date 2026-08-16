"""集成测试夹具：连接本机 5433 上的 Docker PostgreSQL，使用独立测试库。

必须在任何 `import app` 之前设置 DATABASE_URL —— app.py 在模块导入时读取该变量，
且 Flask-SQLAlchemy 3.1 在 init_app() 阶段就创建了 engine。

凭证来源与 app.py 一致：本机 DB_* 放在 backend/.env，由 load_dotenv() 读取
（不覆盖已导出的 shell 变量）。必须先加载 .env 再构造 DSN，否则 DB_PASSWORD
为空，整套集成会在连库时报 fe_sendauth: no password supplied。
"""
import os
import re
import secrets
import sys
from pathlib import Path

import pytest
import sqlalchemy
from dotenv import load_dotenv
from sqlalchemy import text

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

# 与 app.py 同一凭证来源；必须在 _dsn()/DATABASE_URL 之前生效
load_dotenv(BACKEND_DIR / '.env')

TEST_DB_NAME = 'image_search_test'
_SCHEMA_NAME_RE = re.compile(r'^[a-z0-9_]+$')


def _dsn(database):
    user = os.getenv('DB_USER', 'postgres')
    password = os.getenv('DB_PASSWORD', '')
    host = os.getenv('DB_HOST', 'localhost')
    port = os.getenv('DB_PORT', '5433')
    return f'postgresql://{user}:{password}@{host}:{port}/{database}'


def _temporary_schema_name():
    """Return a quoted-identifier-safe schema name for one test invocation."""
    name = f'test_{secrets.token_hex(12)}'
    if not _SCHEMA_NAME_RE.fullmatch(name):
        raise AssertionError('generated schema name is not safe')
    return name


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
    """每个测试使用独立 schema，绝不触碰 public 中的旧表。"""
    from app import create_app
    from models import db

    application = create_app()
    application.config['TESTING'] = True
    application.config['UPLOAD_FOLDER'] = str(tmp_path / 'uploads')
    os.makedirs(application.config['UPLOAD_FOLDER'], exist_ok=True)

    with application.app_context():
        # Do not let a session created by app setup retain the engine binding.
        # The connection below remains open for the whole test so its
        # search_path applies to every ORM request and vector query.
        db.session.remove()
        original_engine = db.engines[None]
        connection = db.engine.connect()
        schema_name = _temporary_schema_name()
        quoted_schema = f'"{schema_name}"'
        try:
            connection.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
            connection.execute(text(f'CREATE SCHEMA {quoted_schema}'))
            connection.execute(text(
                f'SET search_path TO {quoted_schema}, public'
            ))
            connection.commit()

            # Bind active metadata to this same connection.  create_all() is
            # intentionally avoided because it would use the engine pool and
            # could create tables in public instead of this temporary schema.
            # Public may already contain same-named legacy/active tables.  A
            # plain ``create_all(bind=connection)`` would see those through
            # search_path and incorrectly skip creation in this schema; the
            # translation map makes the DDL explicitly target this schema.
            db.metadata.create_all(
                bind=connection.execution_options(
                    schema_translate_map={None: schema_name}
                )
            )
            connection.commit()
            # Flask-SQLAlchemy's Session.get_bind() selects db.engines[None]
            # for mapped models, so replacing that entry is what makes request
            # handlers and db.session share this search_path-bound connection.
            db.engines[None] = connection

            yield application
        finally:
            db.session.remove()
            db.engines[None] = original_engine
            try:
                connection.rollback()
                connection.execute(text(f'DROP SCHEMA {quoted_schema} CASCADE'))
                connection.commit()
            finally:
                connection.close()


@pytest.fixture()
def pg_session_factory(_test_database):
    """真实并发场景用的多会话工厂：独立临时 schema，连接自动携带 search_path。

    与 ``app`` 夹具的单连接技巧不同，取消/领取竞争测试需要多个会话同时持有
    各自的连接，因此 search_path 通过 connect 事件落到每条新连接上。
    """
    from sqlalchemy import event
    from sqlalchemy.orm import sessionmaker

    from models import db

    schema_name = _temporary_schema_name()
    quoted_schema = f'"{schema_name}"'
    engine = sqlalchemy.create_engine(_test_database, pool_pre_ping=True)

    @event.listens_for(engine, 'connect')
    def _set_search_path(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO {quoted_schema}, public')
        cursor.close()

    with engine.connect() as setup_connection:
        setup_connection.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        setup_connection.execute(text(f'CREATE SCHEMA {quoted_schema}'))
        setup_connection.commit()
        db.metadata.create_all(
            bind=setup_connection.execution_options(
                schema_translate_map={None: schema_name}
            )
        )
        setup_connection.commit()

    try:
        yield sessionmaker(bind=engine)
    finally:
        with engine.connect() as cleanup_connection:
            cleanup_connection.execute(
                text(f'DROP SCHEMA {quoted_schema} CASCADE')
            )
            cleanup_connection.commit()
        engine.dispose()
