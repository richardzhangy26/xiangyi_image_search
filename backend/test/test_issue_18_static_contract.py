"""Issue #18 不连接数据库或云端的静态安全合同。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path):
    return (ROOT / path).read_text(encoding='utf-8')


def test_source_identity_is_all_four_existing_columns_and_database_unique():
    ingest = _source('services/asset_ingest.py')
    model = _source('models/image_asset.py')
    schema = _source('../postgres/init/01_init.sql')
    for field in (
        'source_provider',
        'source_bucket',
        'source_relative_path',
        'source_revision',
    ):
        assert field in ingest
        assert field in model
        assert field in schema
    assert 'uq_image_assets_source_identity' in model
    assert 'uq_image_assets_source_identity' in schema


def test_content_reuse_still_persists_a_distinct_source_asset():
    ingest = _source('services/asset_ingest.py')
    reusable_branch = ingest.split('reusable = ImageAsset.query.filter_by(', 1)[1]
    reusable_branch = reusable_branch.split('def _persist(', 1)[0]
    assert 'content_hash=content_hash' in reusable_branch
    assert 'source_relative_path=source_relative_path' in reusable_branch
    assert 'vector_values=list(reusable.vector)' in reusable_branch
    assert "status='active'" in ingest.split('def _persist(', 1)[1]


def test_ingest_never_infers_model_number_from_source_path():
    ingest = _source('services/asset_ingest.py')
    assert 'model_number=prepared.model_number' in ingest
    forbidden = (
        'split_model_number',
        'model_number_from_path',
        "source_relative_path.split('/')[",
    )
    assert all(token not in ingest for token in forbidden)


def test_async_import_does_not_add_retry_cancel_or_destructive_storage():
    ingest = _source('services/asset_ingest.py')
    products = _source('blueprints/products_v2.py')
    combined = ingest + products
    for token in (
        'retry_count',
        'cancel_requested',
        '.delete_object(',
        '.batch_delete_objects(',
    ):
        assert token not in combined


def test_repository_guidance_records_source_identity_and_recycle_bin_semantics():
    guidance = _source('../AGENTS.md')
    assert '来源身份和同一内容返回既有结果' in guidance
    assert '同一来源身份但内容不同返回来源冲突' in guidance
    assert '命中归档来源身份时返回回收站结果' in guidance
    assert '不同来源路径即使内容相同也分别创建资产' in guidance
