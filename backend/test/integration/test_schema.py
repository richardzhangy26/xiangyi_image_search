"""验证独立图片资产表的数据库契约。"""
from sqlalchemy import inspect, text

from models import ImageAsset, Product, db


def _add_product(model_number):
    db.session.add(Product(
        model_number=model_number,
        photographer_file='p',
        alibaba_product_url='https://example.com/1',
        category='相机肩带',
    ))
    db.session.commit()


def _asset(source_relative_path, content_hash, model_number=None):
    return ImageAsset(
        model_number=model_number,
        source_provider='qiniu-kodo',
        source_bucket='xiangxipackage',
        source_relative_path=source_relative_path,
        source_revision=1,
        oss_path=f'image-search/xiangxipackage/{source_relative_path}',
        preview_oss_path=(
            f'image-search/previews/preview-v1/'
            f'{content_hash[:2]}/{content_hash}.jpg'
        ),
        content_hash=content_hash,
        source_size=123,
        source_mime_type='image/png',
        source_width=16,
        source_height=12,
        vector=[0.1] * 1024,
        embedding_model='tongyi-embedding-vision-plus-2026-03-06',
        embedding_dimension=1024,
        normalization_version='preview-v1',
        status='active',
    )


def test_image_asset_schema_has_uuid_vector_fields_and_active_hnsw_index(app):
    inspector = inspect(db.engine)
    schema_name = db.session.execute(
        text('SELECT current_schema()')
    ).scalar_one()
    columns = {
        column['name']: column
        for column in inspector.get_columns('image_assets', schema=schema_name)
    }

    assert set(columns) >= {
        'id',
        'model_number',
        'source_provider',
        'source_bucket',
        'source_relative_path',
        'source_revision',
        'display_name',
        'version',
        'oss_path',
        'preview_oss_path',
        'content_hash',
        'source_size',
        'source_mime_type',
        'source_width',
        'source_height',
        'vector',
        'embedding_model',
        'embedding_dimension',
        'normalization_version',
        'status',
        'archived_at',
        'created_at',
        'updated_at',
    }
    assert str(columns['id']['type']).upper() == 'UUID'
    assert columns['model_number']['nullable'] is True
    assert columns['display_name']['nullable'] is False
    assert columns['version']['nullable'] is False
    assert str(columns['vector']['type']).lower() == 'vector(1024)'

    index_names = {
        row.index_name
        for row in db.session.execute(text(
            "SELECT indexname AS index_name FROM pg_indexes "
            "WHERE schemaname = :schema_name "
            "AND tablename = 'image_assets'"
        ), {'schema_name': schema_name})
    }
    assert {
        'idx_image_assets_content_hash',
        'idx_image_assets_model_number',
        'idx_image_assets_status',
        'idx_image_assets_vector_active_hnsw',
    } <= index_names

    index_definition = db.session.execute(text(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = :schema_name "
        "AND tablename = 'image_assets' "
        "AND indexname = 'idx_image_assets_vector_active_hnsw'"
    ), {'schema_name': schema_name}).scalar_one()
    normalized = ' '.join(index_definition.lower().split())
    assert 'using hnsw' in normalized
    assert 'vector_cosine_ops' in normalized
    assert 'where' in normalized
    assert 'status' in normalized
    assert "'active'" in normalized

    unique_constraints = {
        item['name']
        for item in inspector.get_unique_constraints(
            'image_assets', schema=schema_name
        )
    }
    assert {
        'uq_image_assets_source_identity',
        'image_assets_oss_path_key',
    } <= unique_constraints


def test_image_asset_allows_null_model_number_and_duplicate_content_hash(app):
    digest = 'c' * 64
    db.session.add_all([
        _asset('目录一/同图.png', digest),
        _asset('目录二/同图副本.png', digest),
    ])
    db.session.commit()

    rows = ImageAsset.query.order_by(ImageAsset.source_relative_path).all()
    assert len(rows) == 2
    assert all(row.model_number is None for row in rows)
    assert {row.content_hash for row in rows} == {digest}


def test_deleting_product_detaches_image_asset(app):
    _add_product('M-DETACH')
    asset = _asset('待解绑/图片.png', 'd' * 64, model_number='M-DETACH')
    db.session.add(asset)
    db.session.commit()
    asset_id = asset.id

    db.session.delete(db.session.get(Product, 'M-DETACH'))
    db.session.commit()

    retained = db.session.get(ImageAsset, asset_id)
    assert retained is not None
    assert retained.model_number is None


def test_image_assets_schema_is_exclusive(app):
    schema_name = db.session.execute(
        text('SELECT current_schema()')
    ).scalar_one()
    table_names = set(inspect(db.engine).get_table_names(schema=schema_name))
    assert 'image_assets' in table_names
    assert 'product_images' not in table_names


def test_asset_activity_schema_has_no_foreign_key_to_image_assets(app):
    inspector = inspect(db.engine)
    schema_name = db.session.execute(text('SELECT current_schema()')).scalar_one()
    columns = {
        column['name']
        for column in inspector.get_columns(
            'asset_activity_records', schema=schema_name
        )
    }
    foreign_keys = inspector.get_foreign_keys(
        'asset_activity_records', schema=schema_name
    )

    assert {
        'id', 'event_type', 'target_type', 'target_id', 'request_id',
        'source', 'before_state', 'after_state', 'result', 'created_at',
    } <= columns
    assert foreign_keys == []
