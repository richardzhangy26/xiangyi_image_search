"""Issue #22 恢复/放弃/窗口 API 的单元测试（内存 SQLite，无真实服务）。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app import create_app
from models import AssetActivityRecord, ImageImportItem, db


NOW = datetime.now()


def _build_app():
    app = create_app('testing')
    with app.app_context():
        db.create_all()
    return app


def _seed_item(app, *, status, purge_eligible_at=None, objects_purged_at=None,
               cancel_requested_at=None):
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
            oss_path=f'original/{nonce}',
            preview_oss_path=f'preview/{nonce}',
            content_hash=nonce,
            source_size=10,
            source_mime_type='image/png',
            source_width=2,
            source_height=2,
            normalization_version='preview-v1',
            status=status,
            attempt_count=5,
            request_id='request-22-api',
            purge_eligible_at=purge_eligible_at,
            objects_purged_at=objects_purged_at,
            cancel_requested_at=cancel_requested_at,
        ))
        db.session.commit()
    return item_id


def test_restore_cancelled_item_within_window_requeues_it():
    app = _build_app()
    item_id = _seed_item(
        app,
        status='cancelled',
        purge_eligible_at=NOW + timedelta(days=3),
        cancel_requested_at=NOW - timedelta(days=1),
    )

    response = app.test_client().post(f'/api/image-imports/{item_id}/restore')

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['status'] == 'queued'
    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.status == 'queued'
        assert item.attempt_count == 0
        assert item.cancel_requested_at is None
        assert item.cancelled_at is None
        assert item.purge_eligible_at is None
        activities = AssetActivityRecord.query.filter_by(
            target_id=str(item_id)
        ).all()
        assert any(
            record.event_type == 'image_import.restored'
            for record in activities
        )


def test_restore_after_window_expiry_is_rejected():
    app = _build_app()
    item_id = _seed_item(
        app,
        status='cancelled',
        purge_eligible_at=NOW - timedelta(hours=1),
    )

    response = app.test_client().post(f'/api/image-imports/{item_id}/restore')

    assert response.status_code == 410
    assert response.get_json()['error_code'] == (
        'IMAGE_IMPORT_RESTORE_WINDOW_EXPIRED'
    )
    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.status == 'cancelled'


def test_restore_after_objects_purged_is_rejected():
    app = _build_app()
    item_id = _seed_item(
        app,
        status='cancelled',
        purge_eligible_at=NOW + timedelta(days=1),
        objects_purged_at=NOW,
    )

    response = app.test_client().post(f'/api/image-imports/{item_id}/restore')

    assert response.status_code == 410


def test_restore_non_cancelled_item_is_rejected():
    app = _build_app()
    item_id = _seed_item(app, status='failed')

    response = app.test_client().post(f'/api/image-imports/{item_id}/restore')

    assert response.status_code == 409
    assert response.get_json()['error_code'] == (
        'IMAGE_IMPORT_RESTORE_NOT_ALLOWED'
    )


def test_abandon_failed_item_makes_it_immediately_purge_eligible():
    app = _build_app()
    item_id = _seed_item(
        app,
        status='failed',
        purge_eligible_at=NOW + timedelta(days=29),
    )

    response = app.test_client().post(f'/api/image-imports/{item_id}/abandon')

    assert response.status_code == 200
    assert response.get_json()['status'] == 'abandoned'
    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.status == 'abandoned'
        assert item.purge_eligible_at is not None
        assert item.purge_eligible_at <= datetime.now() + timedelta(seconds=5)
        activities = AssetActivityRecord.query.filter_by(
            target_id=str(item_id)
        ).all()
        assert any(
            record.event_type == 'image_import.abandoned'
            for record in activities
        )


def test_abandon_is_idempotent_for_abandoned_items():
    app = _build_app()
    item_id = _seed_item(app, status='failed')
    client = app.test_client()

    first = client.post(f'/api/image-imports/{item_id}/abandon')
    second = client.post(f'/api/image-imports/{item_id}/abandon')

    assert first.status_code == 200
    assert second.status_code == 200
    with app.app_context():
        activities = AssetActivityRecord.query.filter_by(
            target_id=str(item_id),
            event_type='image_import.abandoned',
        ).all()
        assert len(activities) == 1


def test_abandon_active_item_is_rejected():
    app = _build_app()
    item_id = _seed_item(app, status='queued')

    response = app.test_client().post(f'/api/image-imports/{item_id}/abandon')

    assert response.status_code == 409
    assert response.get_json()['error_code'] == (
        'IMAGE_IMPORT_ABANDON_NOT_ALLOWED'
    )


def test_manual_retry_clears_purge_window_for_recomputation():
    app = _build_app()
    item_id = _seed_item(
        app,
        status='failed',
        purge_eligible_at=NOW + timedelta(days=29),
    )

    response = app.test_client().post(f'/api/image-imports/{item_id}/retry')

    assert response.status_code == 200
    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.status == 'awaiting_retry'
        # 手工重试重新计算窗口：旧到期时刻被清空
        assert item.purge_eligible_at is None


def test_manual_retry_after_objects_purged_is_rejected():
    app = _build_app()
    item_id = _seed_item(
        app,
        status='failed',
        purge_eligible_at=NOW - timedelta(days=1),
        objects_purged_at=NOW,
    )

    response = app.test_client().post(f'/api/image-imports/{item_id}/retry')

    assert response.status_code == 410
    assert response.get_json()['error_code'] == 'IMAGE_IMPORT_RETRY_PURGED'


def test_public_payload_exposes_window_fields():
    app = _build_app()
    item_id = _seed_item(
        app,
        status='cancelled',
        purge_eligible_at=NOW + timedelta(days=7),
    )

    response = app.test_client().get(f'/api/image-imports/{item_id}')

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['purge_eligible_at'] is not None
    assert payload['objects_purged_at'] is None
