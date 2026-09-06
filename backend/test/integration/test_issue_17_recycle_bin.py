"""Issue #17 PostgreSQL scenarios; written only, never run without approval."""

from datetime import datetime, timedelta
from types import SimpleNamespace
import uuid

import pytest

from models import AssetActivityRecord, ImageAsset, Product, db
from services.vector_search import VectorSearchService


pytestmark = pytest.mark.postgresql


FIXED_ARCHIVE_TIME = datetime(2026, 8, 9, 12, 0, 0)


def _product(model_number='CS-001'):
    return Product(
        model_number=model_number,
        photographer_file='摄影师文件',
        alibaba_product_url='https://example.com/product',
        category='挂绳',
    )


def _vector(axis=0):
    value = [0.0] * 1024
    value[axis] = 1.0
    return value


def _asset(
    path,
    *,
    asset_id=None,
    model_number=None,
    status='archived',
    display_name=None,
    archived_at=FIXED_ARCHIVE_TIME,
    vector=None,
):
    digest = uuid.uuid5(uuid.NAMESPACE_URL, path).hex.ljust(64, '0')
    return ImageAsset(
        id=asset_id or uuid.uuid4(),
        model_number=model_number,
        source_provider='qiniu-kodo',
        source_bucket='xiangxipackage',
        source_relative_path=path,
        source_revision=1,
        display_name=display_name or path.rsplit('/', 1)[-1],
        oss_path=f'image-search/xiangxipackage/{path}',
        preview_oss_path=f'image-search/previews/{digest}.jpg',
        content_hash=digest,
        source_size=4096,
        source_mime_type='image/png',
        source_width=1200,
        source_height=800,
        vector=vector or _vector(),
        embedding_model='tongyi-embedding-vision-plus-2026-03-06',
        embedding_dimension=1024,
        normalization_version='preview-v1',
        status=status,
        archived_at=archived_at if status == 'archived' else None,
    )


def _activities(batch_id):
    return AssetActivityRecord.query.filter_by(batch_id=batch_id).all()


def test_archived_list_sorts_searches_both_fields_and_keeps_preview_private(app):
    product = _product()
    same_time = FIXED_ARCHIVE_TIME
    display_match = _asset(
        '普通来源/第一张.png',
        asset_id=uuid.UUID(int=101),
        display_name='业务_100%.png',
        archived_at=same_time,
    )
    source_match = _asset(
        '特殊来源/第二张.png',
        asset_id=uuid.UUID(int=102),
        model_number=product.model_number,
        archived_at=same_time,
    )
    older = _asset(
        '普通来源/更早.png',
        asset_id=uuid.UUID(int=103),
        archived_at=same_time - timedelta(days=1),
    )
    no_time = _asset(
        '普通来源/无时间.png',
        asset_id=uuid.UUID(int=104),
        archived_at=None,
    )
    active = _asset(
        '特殊来源/活跃.png',
        status='active',
        archived_at=None,
    )
    db.session.add_all([
        product, display_match, source_match, older, no_time, active,
    ])
    db.session.commit()

    class FakeStorage:
        def __init__(self):
            self.calls = []

        def sign_download_url(self, path, expires_seconds, *, cache_control=None):
            self.calls.append((path, expires_seconds))
            return SimpleNamespace(
                url='https://signed.example.test/archived-preview',
                expires_at=expires_seconds,
            )

    storage = FakeStorage()
    app.config['IMAGE_ASSET_STORAGE'] = storage
    client = app.test_client()

    listing = client.get('/api/image-assets/archived?per_page=10')
    assert listing.status_code == 200
    body = listing.get_json()
    assert body['total'] == body['archived_total'] == 4
    assert [item['asset_id'] for item in body['assets']] == [
        str(source_match.id),
        str(display_match.id),
        str(older.id),
        str(no_time.id),
    ]
    assert body['assets'][0]['model_number'] == product.model_number
    assert body['assets'][0]['archived_at'] == same_time.isoformat()

    display_search = client.get(
        '/api/image-assets/archived',
        query_string={'search': '业务_100%'},
    ).get_json()
    source_search = client.get(
        '/api/image-assets/archived',
        query_string={'search': '特殊来源'},
    ).get_json()
    assert [item['asset_id'] for item in display_search['assets']] == [
        str(display_match.id),
    ]
    assert display_search['total'] == 1
    assert display_search['archived_total'] == 4
    assert [item['asset_id'] for item in source_search['assets']] == [
        str(source_match.id),
    ]

    preview = client.get(
        source_search['assets'][0]['preview_url'],
        follow_redirects=False,
    )
    assert preview.status_code == 302
    assert preview.headers['Location'] == (
        'https://signed.example.test/archived-preview'
    )
    assert storage.calls == [(
        source_match.preview_oss_path,
        app.config['OSS_SIGNED_URL_TTL_SECONDS'],
    )]


def test_restore_changes_only_lifecycle_fields_and_keeps_identity(app):
    asset = _asset('恢复/身份保持.png', vector=_vector(17))
    db.session.add(asset)
    db.session.commit()
    identity_fields = (
        'id', 'model_number', 'source_provider', 'source_bucket',
        'source_relative_path', 'source_revision', 'display_name', 'oss_path',
        'preview_oss_path', 'content_hash', 'source_size', 'source_mime_type',
        'source_width', 'source_height', 'embedding_model',
        'embedding_dimension', 'normalization_version', 'created_at',
    )
    before_identity = {
        field: getattr(asset, field) for field in identity_fields
    }
    before_vector = list(asset.vector)
    before_version = asset.version

    response = app.test_client().post('/api/image-assets/restore', json={
        'asset_ids': [str(asset.id)],
    })

    assert response.status_code == 200
    body = response.get_json()
    assert body['restored_count'] == 1
    assert body['already_active_count'] == 0
    assert body['items'] == [{
        'asset_id': str(asset.id),
        'status': 'restored',
        'version': before_version + 1,
    }]
    db.session.refresh(asset)
    assert asset.status == 'active'
    assert asset.archived_at is None
    assert asset.version == before_version + 1
    assert {
        field: getattr(asset, field) for field in identity_fields
    } == before_identity
    assert list(asset.vector) == before_vector

    activities = _activities(body['batch_id'])
    assert len(activities) == 2
    assert {activity.event_type for activity in activities} == {
        'asset.restore.batch', 'asset.restore',
    }
    item_activity = next(
        activity for activity in activities
        if activity.event_type == 'asset.restore'
    )
    assert item_activity.before_state['status'] == 'archived'
    assert item_activity.after_state['status'] == 'active'
    assert {
        'vector', 'oss_path', 'preview_oss_path', 'embedding_model',
    }.isdisjoint(item_activity.before_state)


def test_restore_mixed_active_targets_is_idempotent_and_preserves_assignment(app):
    product = _product()
    archived = _asset('恢复/归档.png')
    active = _asset('恢复/已活跃.png', status='active', archived_at=None)
    active_assigned = _asset(
        '恢复/已活跃已归款.png',
        status='active',
        archived_at=None,
        model_number=product.model_number,
    )
    db.session.add_all([product, archived, active, active_assigned])
    db.session.commit()
    requested = [
        str(archived.id), str(active.id), str(active_assigned.id),
    ]
    client = app.test_client()

    first = client.post('/api/image-assets/restore', json={
        'asset_ids': requested,
    })
    assert first.status_code == 200
    assert [item['status'] for item in first.get_json()['items']] == [
        'restored', 'already_active', 'already_active',
    ]
    first_versions = {
        row.id: row.version
        for row in ImageAsset.query.filter(
            ImageAsset.id.in_([archived.id, active.id, active_assigned.id])
        ).all()
    }

    retry = client.post('/api/image-assets/restore', json={
        'asset_ids': requested,
    })

    assert retry.status_code == 200
    assert retry.get_json()['restored_count'] == 0
    assert retry.get_json()['already_active_count'] == 3
    assert [item['status'] for item in retry.get_json()['items']] == [
        'already_active', 'already_active', 'already_active',
    ]
    db.session.expire_all()
    assert {
        row.id: row.version
        for row in ImageAsset.query.filter(
            ImageAsset.id.in_([archived.id, active.id, active_assigned.id])
        ).all()
    } == first_versions
    assert db.session.get(ImageAsset, active_assigned.id).model_number == (
        product.model_number
    )
    retry_activities = _activities(retry.get_json()['batch_id'])
    assert len(retry_activities) == 4
    assert all(
        activity.result == 'noop'
        for activity in retry_activities
        if activity.event_type == 'asset.restore'
    )


def test_restore_conflict_or_duplicate_keeps_the_whole_batch_unchanged(app):
    product = _product()
    eligible = _asset('恢复/保持归档.png')
    assigned = _asset(
        '恢复/已归款归档.png',
        model_number=product.model_number,
    )
    db.session.add_all([product, eligible, assigned])
    db.session.commit()
    missing_id = uuid.uuid4()
    client = app.test_client()

    conflict = client.post('/api/image-assets/restore', json={
        'asset_ids': [str(eligible.id), str(assigned.id), str(missing_id)],
    })

    assert conflict.status_code == 409
    assert conflict.get_json()['error_code'] == 'IMAGE_ASSET_RESTORE_CONFLICT'
    assert {item.get('error_code') for item in conflict.get_json()['items']} >= {
        'IMAGE_ASSET_ALREADY_ASSIGNED', 'IMAGE_ASSET_NOT_FOUND',
    }
    db.session.expire_all()
    assert (
        db.session.get(ImageAsset, eligible.id).status,
        db.session.get(ImageAsset, eligible.id).version,
    ) == ('archived', 1)
    assert db.session.get(ImageAsset, assigned.id).model_number == (
        product.model_number
    )

    duplicate = client.post('/api/image-assets/restore', json={
        'asset_ids': [str(eligible.id), str(eligible.id).upper()],
    })
    assert duplicate.status_code == 409
    assert duplicate.get_json()['items'][0]['error_code'] == (
        'IMAGE_ASSET_DUPLICATE_TARGET'
    )
    db.session.refresh(eligible)
    assert (eligible.status, eligible.version, eligible.archived_at) == (
        'archived', 1, FIXED_ARCHIVE_TIME,
    )


def test_restore_activity_failure_rolls_back_every_asset(app, monkeypatch):
    import models

    first = _asset('恢复/活动失败一.png')
    second = _asset('恢复/活动失败二.png')
    db.session.add_all([first, second])
    db.session.commit()

    class FailingActivity:
        def __init__(self, **_kwargs):
            raise RuntimeError('activity insert failure')

    monkeypatch.setattr(models, 'AssetActivityRecord', FailingActivity)
    response = app.test_client().post('/api/image-assets/restore', json={
        'asset_ids': [str(first.id), str(second.id)],
    })

    assert response.status_code == 500
    assert response.get_json()['error_code'] == 'IMAGE_ASSET_RESTORE_FAILED'
    db.session.refresh(first)
    db.session.refresh(second)
    assert (first.status, first.version, first.archived_at) == (
        'archived', 1, FIXED_ARCHIVE_TIME,
    )
    assert (second.status, second.version, second.archived_at) == (
        'archived', 1, FIXED_ARCHIVE_TIME,
    )


def test_restored_asset_reappears_in_text_vector_and_unassigned_discovery(app):
    asset = _asset(
        '恢复可见/目标图片.png',
        display_name='恢复后可搜索.png',
        vector=_vector(23),
    )
    db.session.add(asset)
    db.session.commit()
    client = app.test_client()

    before_default = client.get('/api/image-assets').get_json()
    before_text = client.get(
        '/api/image-assets', query_string={'search': '恢复后可搜索'}
    ).get_json()
    before_vector = VectorSearchService().search_by_vector(
        _vector(23), top_k=10
    )
    assert str(asset.id) not in {
        item['asset_id'] for item in before_default['assets']
    }
    assert str(asset.id) not in {
        item['asset_id'] for item in before_text['assets']
    }
    assert str(asset.id) not in {item['asset_id'] for item in before_vector}

    restored = client.post('/api/image-assets/restore', json={
        'asset_ids': [str(asset.id)],
    })
    assert restored.status_code == 200

    after_default = client.get('/api/image-assets').get_json()
    after_name = client.get(
        '/api/image-assets', query_string={'search': '恢复后可搜索'}
    ).get_json()
    after_source = client.get(
        '/api/image-assets', query_string={'search': '恢复可见'}
    ).get_json()
    after_vector = VectorSearchService().search_by_vector(
        _vector(23), top_k=10
    )
    assert str(asset.id) in {
        item['asset_id'] for item in after_default['assets']
    }
    assert str(asset.id) in {
        item['asset_id'] for item in after_name['assets']
    }
    assert str(asset.id) in {
        item['asset_id'] for item in after_source['assets']
    }
    assert str(asset.id) in {item['asset_id'] for item in after_vector}
