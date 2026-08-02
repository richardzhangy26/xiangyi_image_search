from sqlalchemy import text

from models import db
from services.legacy_product_images import audit_legacy_product_images


def test_audit_reports_absent_legacy_table(app):
    with app.app_context():
        db.session.execute(text('DROP TABLE IF EXISTS product_images'))
        db.session.commit()
        audit = audit_legacy_product_images(db.session.connection())

    assert audit.table_exists is False
    assert audit.row_count is None
    assert audit.compatibility_required is False
    assert audit.required_actions == ()


def test_audit_reports_empty_legacy_table(app):
    with app.app_context():
        try:
            db.session.execute(text('DROP TABLE IF EXISTS product_images'))
            db.session.commit()
            db.session.execute(text(
                'CREATE TABLE product_images (id integer PRIMARY KEY)'
            ))
            db.session.commit()
            audit = audit_legacy_product_images(db.session.connection())
        finally:
            db.session.execute(text('DROP TABLE IF EXISTS product_images'))
            db.session.commit()

    assert audit.table_exists is True
    assert audit.row_count == 0
    assert audit.compatibility_required is False


def test_audit_requires_manual_migration_for_nonempty_legacy_table(app):
    with app.app_context():
        try:
            db.session.execute(text('DROP TABLE IF EXISTS product_images'))
            db.session.commit()
            db.session.execute(text(
                'CREATE TABLE product_images (id integer PRIMARY KEY)'
            ))
            db.session.execute(text(
                'INSERT INTO product_images (id) VALUES (1)'
            ))
            db.session.commit()
            audit = audit_legacy_product_images(db.session.connection())
        finally:
            db.session.execute(text('DROP TABLE IF EXISTS product_images'))
            db.session.commit()

    assert audit.table_exists is True
    assert audit.row_count == 1
    assert audit.compatibility_required is True
    assert audit.required_actions == (
        '制定独立兼容迁移清单并取得明确授权',
        '在迁移完成前不得 DROP、DELETE 或转换 product_images',
    )
