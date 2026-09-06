"""Issue #21 单项/批量取消 API 的单元测试（内存 SQLite + Flask test client）。"""

from __future__ import annotations

import uuid

from app import create_app
from models import AssetActivityRecord, ImageImportItem, db


CANCELABLE_FIELDS = dict(
    source_provider='image-import-upload',
    source_bucket='image-imports',
    source_relative_path='imports/hash/0001/item.png',
    source_revision=1,
    display_name='item.png',
    oss_path='image-search/imports/original.png',
    preview_oss_path='image-search/imports/preview.jpg',
    content_hash='a1' * 32,
    source_size=123,
    source_mime_type='image/png',
    source_width=40,
    source_height=24,
    normalization_version='preview-v1',
    request_id='request-21-api',
)


def _build_app():
    app = create_app('testing')
    with app.app_context():
        db.create_all()
    return app


def _seed_item(app, *, status, suffix='item'):
    item_id = uuid.uuid4()
    fields = dict(CANCELABLE_FIELDS)
    # 每个种子项使用独立来源身份，避免触发来源唯一约束
    fields['source_relative_path'] = f'imports/hash/0001/{suffix}.png'
    fields['content_hash'] = uuid.uuid4().hex + uuid.uuid4().hex
    with app.app_context():
        db.session.add(ImageImportItem(id=item_id, status=status, **fields))
        db.session.commit()
    return item_id


def test_cancel_queued_item_reaches_cancelled_terminal():
    app = _build_app()
    item_id = _seed_item(app, status='queued')

    response = app.test_client().post(f'/api/image-imports/{item_id}/cancel')

    assert response.status_code == 200
    assert response.get_json()['result'] == 'cancelled'
    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.status == 'cancelled'
        assert item.cancel_requested_at is not None
        assert item.cancelled_at is not None
        activities = AssetActivityRecord.query.filter_by(
            target_id=str(item_id)
        ).all()
        assert any(
            record.event_type == 'image_import.cancelled'
            for record in activities
        )


def test_cancel_embedding_item_persists_intent_without_terminal_state():
    app = _build_app()
    item_id = _seed_item(app, status='embedding')

    response = app.test_client().post(f'/api/image-imports/{item_id}/cancel')

    assert response.status_code == 200
    assert response.get_json()['result'] == 'cancel_requested'
    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.status == 'embedding'
        assert item.cancel_requested_at is not None
        assert item.cancelled_at is None


def test_cancel_awaiting_retry_item_reaches_cancelled_terminal():
    """并集规则：#20 的等待重试项同样可取消（直接落终态）。"""
    from datetime import datetime

    app = _build_app()
    item_id = _seed_item(app, status='awaiting_retry')
    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        item.next_retry_at = datetime(2026, 8, 10, 12, 30, 0)
        db.session.commit()

    response = app.test_client().post(f'/api/image-imports/{item_id}/cancel')

    assert response.status_code == 200
    assert response.get_json()['result'] == 'cancelled'
    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.status == 'cancelled'
        assert item.cancelled_at is not None
        assert item.next_retry_at is None


def test_cancel_completed_item_is_rejected_with_recycle_guidance():
    app = _build_app()
    item_id = _seed_item(app, status='completed')

    response = app.test_client().post(f'/api/image-imports/{item_id}/cancel')

    assert response.status_code == 409
    payload = response.get_json()
    assert payload['error_code'] == 'IMAGE_IMPORT_CANCEL_COMPLETED'
    with app.app_context():
        item = db.session.get(ImageImportItem, item_id)
        assert item.status == 'completed'
        assert item.cancel_requested_at is None


def test_cancel_missing_item_returns_404():
    app = _build_app()

    response = app.test_client().post(
        f'/api/image-imports/{uuid.uuid4()}/cancel'
    )

    assert response.status_code == 404


def test_repeated_cancel_is_idempotent_success():
    app = _build_app()
    item_id = _seed_item(app, status='queued')
    client = app.test_client()

    first = client.post(f'/api/image-imports/{item_id}/cancel')
    second = client.post(f'/api/image-imports/{item_id}/cancel')

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.get_json()['result'] == 'already_cancelled'
    with app.app_context():
        activities = AssetActivityRecord.query.filter_by(
            target_id=str(item_id),
            event_type='image_import.cancelled',
        ).all()
        assert len(activities) == 1


def test_batch_cancel_returns_per_item_results():
    app = _build_app()
    queued_id = _seed_item(app, status='queued', suffix='queued')
    completed_id = _seed_item(app, status='completed', suffix='completed')
    embedding_id = _seed_item(app, status='embedding', suffix='embedding')
    missing_id = uuid.uuid4()

    response = app.test_client().post(
        '/api/image-imports/cancel',
        json={
            'item_ids': [
                str(queued_id), str(completed_id),
                str(embedding_id), str(missing_id),
            ]
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    results = {item['item_id']: item['result'] for item in payload['items']}
    assert results[str(queued_id)] == 'cancelled'
    assert results[str(completed_id)] == 'completed_rejected'
    assert results[str(embedding_id)] == 'cancel_requested'
    assert results[str(missing_id)] == 'not_found'


def test_batch_cancel_rejects_over_limit_and_empty_payload():
    app = _build_app()
    client = app.test_client()

    too_many = [str(uuid.uuid4()) for _ in range(101)]
    over = client.post('/api/image-imports/cancel', json={'item_ids': too_many})
    assert over.status_code == 400
    assert over.get_json()['error_code'] == 'IMAGE_IMPORT_CANCEL_TOO_MANY'

    empty = client.post('/api/image-imports/cancel', json={'item_ids': []})
    assert empty.status_code == 400
    assert empty.get_json()['error_code'] == 'IMAGE_IMPORT_CANCEL_REQUIRED'
