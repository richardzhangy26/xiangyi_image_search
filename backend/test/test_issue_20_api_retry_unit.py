"""Issue #20 手工重试 API 的单元测试（内存 SQLite + Flask test client，无真实服务）。"""

from __future__ import annotations

import uuid

import pytest

from app import create_app
from models import AssetActivityRecord, ImageImportItem, db


def _build_app():
    app = create_app('testing')
    with app.app_context():
        db.create_all()
    return app


def _seed_item(app, *, status, attempt_count=3, cancel_requested_at=None):
    item_id = uuid.uuid4()
    with app.app_context():
        db.session.add(ImageImportItem(
            id=item_id,
            source_provider='image-import-upload',
            source_bucket='image-imports',
            source_relative_path='imports/hash/0001/item.png',
            source_revision=1,
            display_name='item.png',
            oss_path='image-search/imports/original.png',
            preview_oss_path='image-search/imports/preview.jpg',
            content_hash='e' * 64,
            source_size=123,
            source_mime_type='image/png',
            source_width=40,
            source_height=24,
            normalization_version='preview-v1',
            status=status,
            attempt_count=attempt_count,
            cancel_requested_at=cancel_requested_at,
            request_id='request-20-api',
        ))
        db.session.commit()
    return item_id


def test_manual_retry_moves_failed_item_to_immediately_claimable():
    app = _build_app()
    item_id = _seed_item(app, status='failed', attempt_count=3)

    response = app.test_client().post(f'/api/image-imports/{item_id}/retry')

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['status'] == 'awaiting_retry'
    assert payload['item_id'] == str(item_id)
    # 手工重试复用同一任务：不重置已消耗的自动尝试预算
    assert payload['attempt_count'] == 3
    assert payload['next_retry_at'] is not None

    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.status == 'awaiting_retry'
        assert item.claim_token is None
        assert item.lease_expires_at is None
        activities = AssetActivityRecord.query.filter_by(
            target_id=str(item_id)
        ).all()
        assert any(
            record.event_type == 'image_import.manual_retry'
            for record in activities
        )


def test_manual_retry_is_idempotent_for_repeated_clicks():
    app = _build_app()
    item_id = _seed_item(app, status='failed', attempt_count=2)
    client = app.test_client()

    first = client.post(f'/api/image-imports/{item_id}/retry')
    second = client.post(f'/api/image-imports/{item_id}/retry')

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.get_json()['status'] == 'awaiting_retry'

    with app.app_context():
        activities = AssetActivityRecord.query.filter_by(
            target_id=str(item_id),
            event_type='image_import.manual_retry',
        ).all()
        # 重复点击只落一条状态转移记录
        assert len(activities) == 1


def test_manual_retry_of_completed_item_is_rejected():
    app = _build_app()
    item_id = _seed_item(app, status='completed', attempt_count=1)

    response = app.test_client().post(f'/api/image-imports/{item_id}/retry')

    assert response.status_code == 409
    assert response.get_json()['error_code'] == 'IMAGE_IMPORT_RETRY_COMPLETED'

    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.status == 'completed'


def test_manual_retry_of_missing_item_returns_404():
    app = _build_app()

    response = app.test_client().post(
        f'/api/image-imports/{uuid.uuid4()}/retry'
    )

    assert response.status_code == 404
    assert response.get_json()['error_code'] == 'IMAGE_IMPORT_NOT_FOUND'


def test_manual_retry_does_not_reset_in_flight_processing():
    app = _build_app()
    item_id = _seed_item(app, status='embedding', attempt_count=1)

    response = app.test_client().post(f'/api/image-imports/{item_id}/retry')

    assert response.status_code == 200
    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.status == 'embedding'


def test_manual_retry_is_rejected_when_cancel_intent_exists():
    """并集规则：取消意图优先于手工重试。"""
    from datetime import datetime

    app = _build_app()
    item_id = _seed_item(
        app,
        status='failed',
        attempt_count=3,
        cancel_requested_at=datetime(2026, 8, 10, 12, 0, 0),
    )

    response = app.test_client().post(f'/api/image-imports/{item_id}/retry')

    assert response.status_code == 409
    assert response.get_json()['error_code'] == 'IMAGE_IMPORT_RETRY_CANCELLED'
    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.status == 'failed'
        assert item.next_retry_at is None
