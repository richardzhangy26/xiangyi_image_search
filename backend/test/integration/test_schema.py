"""验证 content_hash 列存在且全库唯一。"""
import pytest
from sqlalchemy.exc import IntegrityError

from models import Product, ProductImage, db


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
