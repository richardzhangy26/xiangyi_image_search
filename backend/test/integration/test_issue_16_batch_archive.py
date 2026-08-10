"""Issue #16 PostgreSQL scenarios; not executed without explicit approval."""

import uuid
from types import SimpleNamespace

import pytest

from models import AssetActivityRecord, ImageAsset, Product, db
from services.vector_search import VectorSearchService


pytestmark = pytest.mark.postgresql


def _product(model_number='CS-001'):
    return Product(
        model_number=model_number,
        photographer_file='摄影师文件',
        alibaba_product_url='https://example.com/product',
        category='挂绳',
    )


def _asset(path, *, model_number=None, status='active'):
    digest = uuid.uuid5(uuid.NAMESPACE_URL, path).hex.ljust(64, '0')
    return ImageAsset(
        id=uuid.uuid4(),
        model_number=model_number,
        source_provider='qiniu-kodo',
        source_bucket='xiangxipackage',
        source_relative_path=path,
        source_revision=1,
        display_name=path.rsplit('/', 1)[-1],
        oss_path=f'image-search/xiangxipackage/{path}',
        preview_oss_path=f'image-search/previews/{digest}.jpg',
        content_hash=digest,
        source_size=4096,
        source_mime_type='image/png',
        source_width=1200,
        source_height=800,
        vector=[0.1] * 1024,
        embedding_model='tongyi-embedding-vision-plus-2026-03-06',
        embedding_dimension=1024,
        normalization_version='preview-v1',
        status=status,
    )


def _activities(batch_id):
    return AssetActivityRecord.query.filter_by(batch_id=batch_id).all()


def test_batch_archive_updates_status_time_version_and_audit_atomically(app):
    first, second = _asset('archive/first.png'), _asset('archive/second.png')
    originals = {
        asset.id: (asset.vector, asset.oss_path, asset.preview_oss_path)
        for asset in (first, second)
    }
    db.session.add_all([first, second])
    db.session.commit()

    response = app.test_client().post('/api/image-assets/archive', json={
        'asset_ids': [str(first.id), str(second.id)],
    })

    assert response.status_code == 200
    body = response.get_json()
    rows = ImageAsset.query.filter(ImageAsset.id.in_([first.id, second.id])).all()
    assert all(row.status == 'archived' and row.version == 2 for row in rows)
    assert all(row.archived_at is not None for row in rows)
    assert len({row.archived_at for row in rows}) == 1
    assert set(originals) == {row.id for row in rows}
    for row in rows:
        original_vector, original_oss, original_preview = originals[row.id]
        # pgvector 返回 numpy 数组，不能直接参与 dict/布尔比较
        assert list(row.vector) == list(original_vector)
        assert row.oss_path == original_oss
        assert row.preview_oss_path == original_preview
    activities = _activities(body['batch_id'])
    assert len(activities) == 3
    assert {activity.event_type for activity in activities} == {
        'asset.archive.batch', 'asset.archive'
    }
    forbidden = {'vector', 'oss_path', 'preview_oss_path', 'signature', 'credential'}
    for activity in activities:
        for state in (activity.before_state, activity.after_state):
            assert not state or forbidden.isdisjoint(state)


def test_assigned_or_missing_target_keeps_every_asset_unchanged(app):
    product = _product()
    eligible, assigned = _asset('archive/eligible.png'), _asset(
        'archive/assigned.png', model_number=product.model_number
    )
    db.session.add_all([product, eligible, assigned])
    db.session.commit()

    response = app.test_client().post('/api/image-assets/archive', json={
        'asset_ids': [str(eligible.id), str(assigned.id), str(uuid.uuid4())],
    })

    assert response.status_code == 409
    db.session.expire_all()
    assert (eligible.status, eligible.version, eligible.archived_at) == ('active', 1, None)
    assert (assigned.status, assigned.version, assigned.archived_at) == ('active', 1, None)
    assert {item['error_code'] for item in response.get_json()['items'] if 'error_code' in item} == {
        'IMAGE_ASSET_ALREADY_ASSIGNED', 'IMAGE_ASSET_NOT_FOUND'
    }
    assert len(_activities(response.get_json()['batch_id'])) == 4


def test_duplicate_target_keeps_every_asset_unchanged_and_returns_reasons(app):
    asset = _asset('archive/duplicate.png')
    db.session.add(asset)
    db.session.commit()

    response = app.test_client().post('/api/image-assets/archive', json={
        'asset_ids': [str(asset.id), str(asset.id).upper()],
    })

    assert response.status_code == 409
    db.session.refresh(asset)
    assert (asset.status, asset.version, asset.archived_at) == ('active', 1, None)
    body = response.get_json()
    assert body['items'][0]['error_code'] == 'IMAGE_ASSET_DUPLICATE_TARGET'
    assert len(_activities(body['batch_id'])) == 2


def test_retry_is_idempotent_without_second_version_or_time_change(app):
    asset = _asset('archive/retry.png')
    db.session.add(asset)
    db.session.commit()
    client = app.test_client()

    first = client.post('/api/image-assets/archive', json={'asset_ids': [str(asset.id)]})
    db.session.refresh(asset)
    first_time = asset.archived_at
    second = client.post('/api/image-assets/archive', json={'asset_ids': [str(asset.id)]})

    assert first.status_code == second.status_code == 200
    db.session.refresh(asset)
    assert (asset.version, asset.archived_at) == (2, first_time)
    assert second.get_json()['archived_count'] == 0
    assert second.get_json()['already_archived_count'] == 1
    item = _activities(second.get_json()['batch_id'])[1]
    assert item.result == 'noop'


def test_archived_assets_leave_text_default_assignment_and_vector_results(app):
    archived = _asset('archive/hidden-display-name.png')
    db.session.add(archived)
    db.session.commit()
    response = app.test_client().post('/api/image-assets/archive', json={
        'asset_ids': [str(archived.id)],
    })
    assert response.status_code == 200
    client = app.test_client()

    for url in ('/api/image-assets', '/api/image-assets?search=hidden-display-name',
                '/api/image-assets?search=archive/'):
        assert str(archived.id) not in {item['asset_id'] for item in client.get(url).get_json()['assets']}
    service = VectorSearchService(embedding_client=object(), normalizer=object())
    assert str(archived.id) not in {
        item['asset_id'] for item in service.search_by_vector([0.1] * 1024)
    }


def test_activity_insert_failure_rolls_back_every_asset_update(app, monkeypatch):
    import models

    first, second = _asset('archive/fail-first.png'), _asset('archive/fail-second.png')
    db.session.add_all([first, second])
    db.session.commit()

    class FailingActivity:
        def __init__(self, **_kwargs):
            raise RuntimeError('activity insert failure')

    monkeypatch.setattr(models, 'AssetActivityRecord', FailingActivity)
    response = app.test_client().post('/api/image-assets/archive', json={
        'asset_ids': [str(first.id), str(second.id)],
    })

    assert response.status_code == 500
    db.session.refresh(first)
    db.session.refresh(second)
    assert (first.status, first.version, first.archived_at) == ('active', 1, None)
    assert (second.status, second.version, second.archived_at) == ('active', 1, None)


def test_archived_asset_preview_remains_private_and_available(app):
    asset = _asset('archive/private-preview.png')
    db.session.add(asset)
    db.session.commit()

    class FakeStorage:
        def __init__(self):
            self.calls = []

        def sign_download_url(self, path, expires_seconds, *, cache_control=None):
            self.calls.append((path, expires_seconds))
            return SimpleNamespace(
                url='https://signed.example.test/private-preview',
                expires_at=expires_seconds,
            )

    storage = FakeStorage()
    app.config['IMAGE_ASSET_STORAGE'] = storage
    client = app.test_client()
    assert client.post('/api/image-assets/archive', json={'asset_ids': [str(asset.id)]}).status_code == 200
    preview = client.get(f'/api/image-assets/{asset.id}/preview', follow_redirects=False)

    assert preview.status_code == 302
    assert preview.headers['Location'] == 'https://signed.example.test/private-preview'
    db.session.refresh(asset)
    assert asset.status == 'archived'
    assert storage.calls == [(asset.preview_oss_path, app.config['OSS_SIGNED_URL_TTL_SECONDS'])]
