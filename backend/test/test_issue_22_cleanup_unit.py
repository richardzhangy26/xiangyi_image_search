"""Issue #22 引用安全清理的单元测试（SQLite + 伪存储，绝不触碰真实 OSS）。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app import create_app
from models import AssetActivityRecord, ImageAsset, ImageImportItem, db
from services.import_cleanup import (
    cleanup_expired_imports,
    cleanup_one_item,
    count_object_references,
)
from services.object_storage import ObjectStorageError


NOW = datetime(2026, 8, 20, 12, 0, 0)


class FakeCleaningStorage:
    def __init__(self, existing=None, fail_keys=()):
        self.existing = set(existing or ())
        self.fail_keys = set(fail_keys)
        self.deleted = []

    def delete_object(self, key):
        if key in self.fail_keys:
            raise ObjectStorageError('模拟删除失败')
        self.deleted.append(key)
        if key in self.existing:
            self.existing.discard(key)
            return 'deleted'
        return 'already_gone'


def _build_app():
    app = create_app('testing')
    with app.app_context():
        db.create_all()
    return app


def _seed_item(app, *, status, purge_eligible_at, original, preview,
               objects_purged_at=None):
    item_id = uuid.uuid4()
    nonce = uuid.uuid4().hex
    with app.app_context():
        db.session.add(ImageImportItem(
            id=item_id,
            source_provider='image-import-upload',
            source_bucket='image-imports',
            source_relative_path=f'imports/{nonce}/item.png',
            source_revision=1,
            display_name='item.png',
            oss_path=original,
            preview_oss_path=preview,
            content_hash=nonce,
            source_size=10,
            source_mime_type='image/png',
            source_width=2,
            source_height=2,
            normalization_version='preview-v1',
            status=status,
            request_id='request-22',
            purge_eligible_at=purge_eligible_at,
            objects_purged_at=objects_purged_at,
        ))
        db.session.commit()
    return item_id


def _seed_asset(app, *, original=None, preview=None, status='active'):
    asset_id = uuid.uuid4()
    nonce = uuid.uuid4().hex
    with app.app_context():
        db.session.add(ImageAsset(
            id=asset_id,
            model_number=None,
            source_provider='image-import-upload',
            source_bucket='image-imports',
            source_relative_path=f'asset/{nonce}.png',
            source_revision=1,
            display_name='asset.png',
            version=1,
            oss_path=original or f'original/{nonce}',
            preview_oss_path=preview or f'preview/{nonce}',
            content_hash=nonce,
            source_size=10,
            source_mime_type='image/png',
            source_width=2,
            source_height=2,
            vector=[0.1] * 1024,
            embedding_model='tongyi-embedding-vision-plus-2026-03-06',
            embedding_dimension=1024,
            normalization_version='preview-v1',
            status=status,
        ))
        db.session.commit()
    return asset_id


def test_expired_cancelled_item_without_references_is_fully_purged():
    app = _build_app()
    item_id = _seed_item(
        app,
        status='cancelled',
        purge_eligible_at=NOW - timedelta(hours=1),
        original='imports/orig/a',
        preview='imports/preview/a',
    )
    storage = FakeCleaningStorage(
        existing={'imports/orig/a', 'imports/preview/a'}
    )

    with app.app_context():
        processed = cleanup_expired_imports(db.session, storage=storage, now=NOW)

    assert processed == 1
    assert storage.deleted == ['imports/orig/a', 'imports/preview/a']
    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.objects_purged_at == NOW
        events = {
            record.event_type
            for record in AssetActivityRecord.query.filter_by(
                target_id=str(item_id)
            ).all()
        }
        assert 'image_import.expired' in events
        assert 'image_import.objects_purged' in events


def test_original_referenced_by_active_asset_is_kept():
    app = _build_app()
    item_id = _seed_item(
        app,
        status='failed',
        purge_eligible_at=NOW - timedelta(hours=1),
        original='shared/orig',
        preview='unique/preview',
    )
    _seed_asset(app, original='shared/orig')
    storage = FakeCleaningStorage(existing={'shared/orig', 'unique/preview'})

    with app.app_context():
        cleanup_expired_imports(db.session, storage=storage, now=NOW)

    # 原图被正式资产引用必须保留；无引用的预览被删除
    assert storage.deleted == ['unique/preview']
    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.objects_purged_at == NOW


def test_recycle_bin_asset_reference_protects_preview_forever():
    app = _build_app()
    _seed_item(
        app,
        status='abandoned',
        purge_eligible_at=NOW,
        original='unique/orig',
        preview='shared/preview',
    )
    _seed_asset(app, preview='shared/preview', status='archived')
    storage = FakeCleaningStorage(existing={'unique/orig', 'shared/preview'})

    with app.app_context():
        cleanup_expired_imports(db.session, storage=storage, now=NOW)

    # 回收站（archived）资产的引用永远保护共享预览
    assert storage.deleted == ['unique/orig']


def test_shared_reference_between_import_items_blocks_deletion_until_last_one():
    app = _build_app()
    first = _seed_item(
        app,
        status='cancelled',
        purge_eligible_at=NOW - timedelta(hours=2),
        original='shared/orig-2',
        preview='shared/preview-2',
    )
    second = _seed_item(
        app,
        status='cancelled',
        purge_eligible_at=NOW - timedelta(hours=1),
        original='shared/orig-2',
        preview='shared/preview-2',
    )
    storage = FakeCleaningStorage(
        existing={'shared/orig-2', 'shared/preview-2'}
    )

    with app.app_context():
        processed = cleanup_expired_imports(db.session, storage=storage, now=NOW)

    # 第一个项清理时第二个项仍是活引用：对象保留；第二个项清理时删除。
    assert processed == 2
    assert storage.deleted == ['shared/orig-2', 'shared/preview-2']
    with app.app_context():
        first_item = db.session.get(ImageImportItem, first)
        assert first_item.objects_purged_at == NOW
        purge_record = AssetActivityRecord.query.filter_by(
            target_id=str(first),
            event_type='image_import.objects_purged',
        ).one()
        assert purge_record.after_state['objects'] == {
            'original': 'kept_referenced',
            'preview': 'kept_referenced',
        }


def test_not_yet_due_and_completed_items_are_never_cleaned():
    app = _build_app()
    _seed_item(
        app,
        status='cancelled',
        purge_eligible_at=NOW + timedelta(days=1),
        original='future/orig',
        preview='future/preview',
    )
    storage = FakeCleaningStorage(existing={'future/orig', 'future/preview'})

    with app.app_context():
        processed = cleanup_expired_imports(db.session, storage=storage, now=NOW)

    assert processed == 0
    assert storage.deleted == []


def test_cleanup_is_idempotent_for_already_purged_items():
    app = _build_app()
    item_id = _seed_item(
        app,
        status='cancelled',
        purge_eligible_at=NOW - timedelta(hours=1),
        original='gone/orig',
        preview='gone/preview',
        objects_purged_at=NOW - timedelta(minutes=5),
    )
    storage = FakeCleaningStorage()

    with app.app_context():
        processed = cleanup_expired_imports(db.session, storage=storage, now=NOW)

    assert processed == 0
    assert storage.deleted == []
    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.objects_purged_at == NOW - timedelta(minutes=5)


def test_delete_failure_keeps_item_eligible_for_next_run():
    app = _build_app()
    item_id = _seed_item(
        app,
        status='cancelled',
        purge_eligible_at=NOW - timedelta(hours=1),
        original='flaky/orig',
        preview='flaky/preview',
    )
    storage = FakeCleaningStorage(
        existing={'flaky/orig', 'flaky/preview'},
        fail_keys={'flaky/orig'},
    )

    with app.app_context():
        processed = cleanup_expired_imports(db.session, storage=storage, now=NOW)

    assert processed == 0
    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.objects_purged_at is None

    # 第二次运行（故障恢复）完成清理
    storage.fail_keys.clear()
    with app.app_context():
        processed = cleanup_expired_imports(db.session, storage=storage, now=NOW)
    assert processed == 1
    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.objects_purged_at == NOW


def test_missing_object_counts_as_successful_cleanup():
    app = _build_app()
    item_id = _seed_item(
        app,
        status='abandoned',
        purge_eligible_at=NOW,
        original='missing/orig',
        preview='missing/preview',
    )
    storage = FakeCleaningStorage(existing=set())

    with app.app_context():
        cleanup_one_item(db.session, item_id, storage=storage, now=NOW)

    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.objects_purged_at == NOW
        record = AssetActivityRecord.query.filter_by(
            target_id=str(item_id),
            event_type='image_import.objects_purged',
        ).one()
        assert record.after_state['objects'] == {
            'original': 'already_gone',
            'preview': 'already_gone',
        }
        # abandoned 不产生自然到期事件
        expired = AssetActivityRecord.query.filter_by(
            target_id=str(item_id),
            event_type='image_import.expired',
        ).count()
        assert expired == 0


def test_reference_count_excludes_purged_items_and_self():
    app = _build_app()
    item_id = _seed_item(
        app,
        status='cancelled',
        purge_eligible_at=NOW - timedelta(hours=1),
        original='count/orig',
        preview='count/preview',
    )
    _seed_item(
        app,
        status='cancelled',
        purge_eligible_at=NOW - timedelta(hours=1),
        original='count/orig',
        preview='count/preview',
        objects_purged_at=NOW - timedelta(hours=1),
    )

    with app.app_context():
        references = count_object_references(
            db.session,
            key='count/orig',
            asset_column=ImageAsset.oss_path,
            item_column=ImageImportItem.oss_path,
            exclude_item_id=item_id,
        )
    assert references == 0
