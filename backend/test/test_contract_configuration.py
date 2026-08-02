"""运行时与新数据库部署契约测试。"""

from pathlib import Path

import pytest
from sqlalchemy import inspect


def repo_file(relative_path):
    """Resolve repository files independently from the current working directory."""
    return Path(__file__).resolve().parents[2] / relative_path


@pytest.fixture()
def app():
    from app import create_app
    from models import db

    application = create_app('testing')
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def test_app_does_not_expose_legacy_uploads_route(client):
    response = client.get('/uploads/product_images/legacy.png')
    assert response.status_code == 404


def test_new_database_schema_does_not_create_product_images(app):
    from models import db

    with app.app_context():
        table_names = set(inspect(db.engine).get_table_names())
    assert 'image_assets' in table_names
    assert 'product_images' not in table_names


def test_deployment_files_do_not_persist_or_proxy_legacy_uploads():
    compose = repo_file('docker-compose.yml').read_text(encoding='utf-8')
    nginx = repo_file('frontend/nginx.conf').read_text(encoding='utf-8')
    init_sql = repo_file('postgres/init/01_init.sql').read_text(encoding='utf-8')
    assert './backend/uploads:/app/uploads' not in compose
    assert 'location /uploads/' not in nginx
    assert 'CREATE TABLE IF NOT EXISTS product_images' not in init_sql
