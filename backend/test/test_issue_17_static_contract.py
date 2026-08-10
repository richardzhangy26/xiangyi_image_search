"""Static safety contracts for Issue #17 recycle-bin boundaries."""

import ast
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def test_archived_list_and_restore_have_independent_routes_and_error_contracts():
    source = _read(BACKEND_DIR / 'blueprints' / 'image_assets.py')

    assert "@image_assets_bp.get('/archived')" in source
    assert 'list_archived_image_assets(' in source
    assert 'INVALID_IMAGE_ASSET_ARCHIVED_LIST_PARAMS' in source
    assert "@image_assets_bp.post('/restore')" in source
    assert 'restore_image_assets(' in source
    assert 'INVALID_IMAGE_ASSET_RESTORE_BATCH' in source
    assert 'IMAGE_ASSET_RESTORE_CONFLICT' in source
    assert 'IMAGE_ASSET_RESTORE_FAILED' in source


def test_every_non_recycle_bin_discovery_query_keeps_an_active_filter():
    management = _read(BACKEND_DIR / 'blueprints' / 'image_assets.py')
    vector = _read(BACKEND_DIR / 'services' / 'vector_search.py')
    products = _read(BACKEND_DIR / 'blueprints' / 'products_v2.py')

    assert "ImageAsset.query.filter(ImageAsset.status == 'active')" in management
    assert "WHERE status = 'active'" in vector
    assert "ImageAsset.status == 'active'" in products
    assert "if any(asset.status != 'active'" in management


def test_recycle_bin_query_has_dual_counts_literal_search_and_fixed_order():
    source = _read(BACKEND_DIR / 'services' / 'asset_recycle_bin.py')

    assert "ImageAsset.status == 'archived'" in source
    assert 'ImageAsset.display_name.ilike' in source
    assert 'ImageAsset.source_relative_path.ilike' in source
    assert "escape='\\\\'" in source
    assert 'ImageAsset.archived_at.desc().nullslast()' in source
    assert 'ImageAsset.id.desc()' in source
    assert 'archived_total' in source
    assert 'total' in source


def test_restore_update_only_writes_allowed_lifecycle_fields():
    source = _read(BACKEND_DIR / 'services' / 'asset_recycle_bin.py')
    tree = ast.parse(source)
    value_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'values'
    ]

    assert len(value_calls) == 1
    assert value_calls[0].args == []
    assert {keyword.arg for keyword in value_calls[0].keywords} == {
        'status', 'archived_at', 'updated_at', 'version',
    }
    assert "status='active'" in source
    assert 'archived_at=None' in source
    assert 'updated_at=func.now()' in source
    assert 'version=ImageAsset.version + 1' in source
    assert "ImageAsset.status == 'archived'" in source
    assert 'ImageAsset.model_number.is_(None)' in source


def test_recycle_bin_module_has_no_delete_storage_embedding_or_purge_path():
    source = _read(BACKEND_DIR / 'services' / 'asset_recycle_bin.py')
    lowered = source.lower()

    assert 'session.delete' not in source
    assert 'OssObjectStorage' not in source
    assert 'EmbeddingClient' not in source
    assert '.oss_path =' not in source
    assert '.preview_oss_path =' not in source
    assert '.vector =' not in source
    assert 'delete(' not in lowered
    assert 'purge' not in lowered
    assert 'expiry' not in lowered
    assert 'expiration' not in lowered
    assert 'permanent' not in lowered
