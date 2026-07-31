"""验证旧产品图片表与独立图片资产表的数据库契约。"""
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from models import ImageAsset, Product, ProductImage, db


def _add_product(model_number):
    db.session.add(Product(
        model_number=model_number,
        photographer_file='p',
        alibaba_product_url='https://example.com/1',
        category='相机肩带',
    ))
    db.session.commit()


def test_content_hash_column_accepts_value(app):
    _add_product('M-001')
    db.session.add(ProductImage(
        model_number='M-001',
        image_path='/uploads/product_images/M-001/aaaa.jpg',
        vector=[0.1] * 1024,
        content_hash='a' * 64,
    ))
    db.session.commit()

    row = ProductImage.query.one()
    assert row.content_hash == 'a' * 64
    assert row.to_dict()['content_hash'] == 'a' * 64


def test_duplicate_content_hash_rejected_across_different_products(app):
    """全库唯一：同一张图出现在两个型号下也必须被拒绝。"""
    _add_product('M-001')
    _add_product('M-002')

    db.session.add(ProductImage(
        model_number='M-001',
        image_path='/uploads/product_images/M-001/aaaa.jpg',
        vector=[0.1] * 1024,
        content_hash='b' * 64,
    ))
    db.session.commit()

    db.session.add(ProductImage(
        model_number='M-002',
        image_path='/uploads/product_images/M-002/aaaa.jpg',
        vector=[0.2] * 1024,
        content_hash='b' * 64,
    ))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_null_content_hash_allowed_multiple_times(app):
    """UNIQUE 索引允许多个 NULL —— 旧数据不会因为迁移而炸掉。"""
    _add_product('M-001')
    db.session.add(ProductImage(
        model_number='M-001', image_path='/uploads/a.jpg',
        vector=[0.1] * 1024, content_hash=None,
    ))
    db.session.add(ProductImage(
        model_number='M-001', image_path='/uploads/b.jpg',
        vector=[0.1] * 1024, content_hash=None,
    ))
    db.session.commit()

    assert ProductImage.query.count() == 2


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
    columns = {column['name']: column for column in inspector.get_columns('image_assets')}

    assert set(columns) >= {
        'id',
        'model_number',
        'source_provider',
        'source_bucket',
        'source_relative_path',
        'source_revision',
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
    assert str(columns['vector']['type']).lower() == 'vector(1024)'

    index_names = {
        row.index_name
        for row in db.session.execute(text(
            "SELECT indexname AS index_name FROM pg_indexes "
            "WHERE tablename = 'image_assets'"
        ))
    }
    assert {
        'idx_image_assets_content_hash',
        'idx_image_assets_model_number',
        'idx_image_assets_status',
        'idx_image_assets_vector_active_hnsw',
    } <= index_names

    index_definition = db.session.execute(text(
        "SELECT indexdef FROM pg_indexes "
        "WHERE tablename = 'image_assets' "
        "AND indexname = 'idx_image_assets_vector_active_hnsw'"
    )).scalar_one()
    normalized = ' '.join(index_definition.lower().split())
    assert 'using hnsw' in normalized
    assert 'vector_cosine_ops' in normalized
    assert 'where' in normalized
    assert 'status' in normalized
    assert "'active'" in normalized

    unique_constraints = {
        item['name']
        for item in inspector.get_unique_constraints('image_assets')
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


def test_deleting_product_detaches_image_asset_without_touching_legacy_table(app):
    _add_product('M-DETACH')
    asset = _asset('待解绑/图片.png', 'd' * 64, model_number='M-DETACH')
    legacy = ProductImage(
        model_number='M-DETACH',
        image_path='/uploads/product_images/M-DETACH/legacy.jpg',
        vector=[0.2] * 1024,
        content_hash='e' * 64,
    )
    db.session.add_all([asset, legacy])
    db.session.commit()
    asset_id = asset.id

    db.session.delete(db.session.get(Product, 'M-DETACH'))
    db.session.commit()

    retained = db.session.get(ImageAsset, asset_id)
    assert retained is not None
    assert retained.model_number is None
    assert ProductImage.query.count() == 0


def test_image_assets_and_product_images_tables_coexist(app):
    tables = set(inspect(db.engine).get_table_names())
    assert {'image_assets', 'product_images'} <= tables
