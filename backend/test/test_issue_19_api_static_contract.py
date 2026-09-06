"""Issue #19 导入 HTTP 边界的静态合同。"""

from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8') if path.exists() else ''


def test_import_api_has_persistent_create_list_and_detail_routes():
    source = _read(BACKEND_DIR / 'blueprints' / 'image_imports.py')

    assert "url_prefix='/api/image-imports'" in source
    assert "@image_imports_bp.post('')" in source
    assert "@image_imports_bp.get('')" in source
    assert "@image_imports_bp.get('/<uuid:item_id>')" in source
    assert "request.files.getlist('images')" in source
    assert 'MAX_IMPORT_FILES = 20' in source
    # #19 合同意图：创建路由必须经持久队列服务（不是同步 ingest）。#27 允许
    # 请求级 chunk-owner 入口 queue_many_caller_owned（工厂未注入时逐图委托
    # queue_one），两种入口都必须继续落到 image_import_items 持久队列。
    assert (
        'service.queue_one(' in source
        or 'service.queue_many_caller_owned(' in source
    )
    assert "status_code = 202 if queued_count else 200" in source
    assert 'unresolved_count' in source
    assert 'processing_count' in source
    assert '.to_public_dict()' in source


def test_import_http_path_never_calls_embedding_or_exposes_private_state():
    source = _read(BACKEND_DIR / 'blueprints' / 'image_imports.py')
    lowered = source.lower()

    assert 'embed_normalized' not in source
    assert 'threading' not in lowered
    assert 'threadpool' not in lowered
    assert 'preview_oss_path' not in source
    assert 'oss_path' not in source
    assert 'signed_url' not in lowered
    assert "'vector'" not in source
    assert "'failed'" in source
    # Issue #20 取代原「无 retry」禁令：手工重试端点存在且幂等
    assert "@image_imports_bp.post('/<uuid:item_id>/retry')" in source
    # Issue #21 取代原「无 cancel」禁令：单项/批量取消端点存在且幂等
    assert "@image_imports_bp.post('/<uuid:item_id>/cancel')" in source
    assert "@image_imports_bp.post('/cancel')" in source


def test_app_registers_import_blueprint_without_running_migration():
    source = _read(BACKEND_DIR / 'app.py')

    assert 'from blueprints.image_imports import image_imports_bp' in source
    assert 'app.register_blueprint(image_imports_bp)' in source
    assert 'issue_19_image_import_items' not in source


def test_product_upload_source_construction_is_shared_without_changing_route():
    product = _read(BACKEND_DIR / 'blueprints' / 'products_v2.py')
    shared = _read(BACKEND_DIR / 'services' / 'upload_source.py')

    assert 'prepare_multipart_source' in shared
    assert 'prepare_multipart_source(' in product
    assert "f'models/{encoded_model_number}/{content_hash}/'" in product
    assert "f'{occurrence:04d}/{filename}'" in product

