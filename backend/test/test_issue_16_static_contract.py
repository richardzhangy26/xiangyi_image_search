"""Static contracts for Issue #16 server-side archive boundaries."""

from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _read(path):
    return path.read_text(encoding='utf-8')


def test_archive_route_delegates_to_the_transaction_module():
    source = _read(BACKEND_DIR / 'blueprints' / 'image_assets.py')
    assert "@image_assets_bp.post('/archive')" in source
    assert 'archive_unassigned_image_assets(' in source
    assert 'IMAGE_ASSET_ARCHIVE_CONFLICT' in source
    assert 'IMAGE_ASSET_ARCHIVE_FAILED' in source


def test_every_discovery_query_keeps_an_explicit_active_filter():
    management = _read(BACKEND_DIR / 'blueprints' / 'image_assets.py')
    vector = _read(BACKEND_DIR / 'services' / 'vector_search.py')
    products = _read(BACKEND_DIR / 'blueprints' / 'products_v2.py')
    assert "ImageAsset.query.filter(ImageAsset.status == 'active')" in management
    assert "WHERE status = 'active'" in vector
    assert "ImageAsset.status == 'active'" in products
    assert "if any(asset.status != 'active'" in management


def test_archive_module_has_no_delete_storage_or_embedding_path():
    source = _read(BACKEND_DIR / 'services' / 'asset_archive.py')
    assert 'session.delete' not in source
    assert 'OssObjectStorage' not in source
    assert 'EmbeddingClient' not in source
    assert '.oss_path =' not in source
    assert '.preview_oss_path =' not in source
    assert '.vector =' not in source


def test_assignment_locks_assets_in_a_stable_uuid_order():
    source = _read(BACKEND_DIR / 'blueprints' / 'image_assets.py')
    assignment = source[source.index("@image_assets_bp.post('/assign')"):]
    assert '.order_by(ImageAsset.id).with_for_update()' in assignment
