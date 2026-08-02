"""运行时与新数据库部署契约测试。"""

from pathlib import Path
import importlib.util

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


def test_retired_code_is_not_importable_or_referenced():
    assert importlib.util.find_spec('services.ingest') is None
    assert importlib.util.find_spec('blueprints.oss') is None
    assert not repo_file('backend/scripts/batch_upload_oss.py').exists()
    assert not repo_file('backend/scripts/oss_uploader.py').exists()
    assert not repo_file('backend/blueprints/products.py').exists()
    assert not repo_file('backend/create_tables.sql').exists()
    assert not repo_file('backend/services/parse_service.py').exists()
    active_sources = [
        repo_file('backend/app.py'),
        repo_file('backend/blueprints/products_v2.py'),
        repo_file('backend/models/__init__.py'),
    ]
    assert all(
        'ProductImage' not in source.read_text(encoding='utf-8')
        for source in active_sources
    )


def test_legacy_ingest_write_mode_refuses_before_scanning(monkeypatch, tmp_path):
    from scripts import ingest_images

    monkeypatch.setattr(
        ingest_images,
        'scan_directory',
        lambda _root: pytest.fail('禁用检查后不应继续扫描'),
    )

    with pytest.raises(
        ingest_images.LegacyProductImageIngestDisabledError,
        match='已停用',
    ):
        ingest_images.run(object(), str(tmp_path), dry_run=False)


def test_operational_docs_name_oss_as_authoritative_store():
    agents = repo_file('AGENTS.md').read_text(encoding='utf-8')
    migration = repo_file(
        'backend/scripts/README_OSS_MIGRATION.md'
    ).read_text(encoding='utf-8')
    assert 'OSS 已成为正式图片源' in agents
    assert 'Kodo 只读备份' in agents
    assert 'python -m scripts.audit_legacy_product_images' in migration
    assert 'python -m scripts.ingest_images --root' not in agents
