"""Product 写路径统一使用 ImageAsset 与私有 OSS 的集成契约。"""
import io
import hashlib
import json
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image

from models import ImageAsset, Product, db
from services.embedding import EmbeddingServiceError
from services.legacy_product_images import LegacyProductImagesAudit
from services.object_storage import (
    ObjectStorageError,
    SignedDownloadUrl,
    StoredObject,
)


def _png_bytes(color):
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), color).save(buffer, format='PNG')
    return buffer.getvalue()


@dataclass
class _FakeStoredObject:
    data: bytes
    content_type: str
    metadata: dict[str, str]
    etag: str


class FakeAssetStorage:
    def __init__(self):
        self.objects: dict[str, _FakeStoredObject] = {}
        self.uploaded_keys: list[str] = []

    def head_object(self, key):
        item = self.objects.get(key)
        if item is None:
            return None
        return StoredObject(
            key=key,
            size=len(item.data),
            content_type=item.content_type,
            metadata=item.metadata,
            etag=item.etag,
        )

    def put_file(self, key, source_path, *, spec):
        with open(source_path, 'rb') as source:
            self._put(key, source.read(), spec)

    def put_bytes(self, key, data, *, spec):
        self._put(key, data, spec)

    def sign_download_url(self, key, expires_seconds, *, cache_control=None):
        return SignedDownloadUrl(
            url=f'https://private.example/{key}?expires={expires_seconds}',
            expires_at=int(time.time()) + expires_seconds,
        )

    def _put(self, key, data, spec):
        self.uploaded_keys.append(key)
        self.objects[key] = _FakeStoredObject(
            data=bytes(data),
            content_type=spec.content_type,
            metadata=dict(spec.metadata),
            etag=spec.md5_hex,
        )


class FailingAssetStorage(FakeAssetStorage):
    def put_file(self, key, source_path, *, spec):
        raise ObjectStorageError('secret storage failure')


class ConflictingAssetStorage(FakeAssetStorage):
    def head_object(self, key):
        return StoredObject(
            key=key,
            size=1,
            content_type='application/octet-stream',
            metadata={},
            etag='wrong',
        )


class FakeAssetEmbedding:
    def __init__(self, fail_on_call=None):
        self.fail_on_call = fail_on_call
        self.calls = 0
        self.payloads = []

    def embed_normalized_image(self, image_path, request_id=None):
        self.calls += 1
        with open(image_path, 'rb') as source:
            self.payloads.append(source.read())
        if self.fail_on_call == self.calls:
            raise EmbeddingServiceError('secret upstream failure')
        return np.full(1024, 0.1, dtype=np.float32)


def _install_asset_dependencies(app, *, storage=None, embedding=None):
    storage = storage or FakeAssetStorage()
    embedding = embedding or FakeAssetEmbedding()
    app.config['IMAGE_ASSET_STORAGE'] = storage
    app.config['IMAGE_INGEST_EMBEDDING'] = embedding
    return storage, embedding


def _product_payload(model_number):
    return json.dumps({
        'model_number': model_number,
        'photographer_file': 'p',
        'alibaba_product_url': 'https://example.com/x',
        'category': '相机肩带',
    })


def test_create_product_requires_only_model_number(app):
    client = app.test_client()

    response = client.post('/api/products', data={
        'product': json.dumps({'model_number': 'MIN-001'}),
    }, content_type='multipart/form-data')

    assert response.status_code == 201
    assert response.get_json()['model_number'] == 'MIN-001'
    db.session.expire_all()
    product = db.session.get(Product, 'MIN-001')
    assert product is not None
    assert product.photographer_file == ''
    assert product.alibaba_product_url == ''
    assert product.category == ''

    missing_model = client.post('/api/products', data={
        'product': json.dumps({'photographer_file': 'p'}),
    }, content_type='multipart/form-data')
    assert missing_model.status_code == 400


def test_create_product_uploads_private_image_asset_and_reads_stable_preview(app):
    storage, embedding = _install_asset_dependencies(app)
    client = app.test_client()
    original = _png_bytes('red')

    response = client.post('/api/products', data={
        'product': _product_payload('CS-ASSET-001'),
        'images': [(io.BytesIO(original), '中文 主图.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 201
    response_body = response.get_json()
    assert response_body['uploaded_images'] == 1
    assert response_body['reused_images'] == 0
    assert response_body['skipped_duplicates'] == []
    assert [item['status'] for item in response_body['image_results']] == [
        'created'
    ]

    detail = client.get('/api/products/CS-ASSET-001')
    assert detail.status_code == 200
    images = detail.get_json()['images']
    assert len(images) == 1
    assert images[0]['image_path'] == (
        f"/api/image-assets/{images[0]['id']}/preview"
    )
    assert images[0]['source_relative_path'].endswith('/中文 主图.png')
    assert images[0]['display_name'] == '中文 主图.png'
    assert images[0]['version'] == 1
    assert images[0]['original_path'] is None

    preview = client.get(images[0]['image_path'])
    assert preview.status_code == 302
    assert preview.headers['Location'].startswith('https://private.example/')

    asset = ImageAsset.query.one()
    assert asset.model_number == 'CS-ASSET-001'
    assert asset.source_provider == 'product-upload'
    assert storage.objects[asset.oss_path].data == original
    assert hashlib.sha256(original).hexdigest() == asset.content_hash
    assert len(embedding.payloads) == 1

    listed = client.get('/api/products?page=0')
    assert listed.status_code == 200
    assert listed.get_json()['products'][0]['images'] == images

def test_update_product_creates_distinct_asset_and_reuses_compatible_content(app):
    storage, embedding = _install_asset_dependencies(app)
    client = app.test_client()
    original = _png_bytes('blue')

    first = client.post('/api/products', data={
        'product': _product_payload('CS-ASSET-001'),
        'images': [(io.BytesIO(original), 'first.png')],
    }, content_type='multipart/form-data')
    second_product = client.post('/api/products', data={
        'product': _product_payload('CS-ASSET-002'),
    }, content_type='multipart/form-data')

    response = client.put('/api/products/CS-ASSET-002', data={
        'product': json.dumps({'photographer_file': 'changed'}),
        'images': [(io.BytesIO(original), 'second.png')],
    }, content_type='multipart/form-data')

    assert first.status_code == 201
    assert second_product.status_code == 201
    assert response.status_code == 200
    assert response.get_json()['uploaded_images'] == 1

    first_image = client.get('/api/products/CS-ASSET-001').get_json()['images'][0]
    second_detail = client.get('/api/products/CS-ASSET-002').get_json()
    second_image = second_detail['images'][0]
    assert first_image['id'] != second_image['id']
    assert second_detail['photographer_file'] == 'changed'

    assets = ImageAsset.query.order_by(ImageAsset.created_at).all()
    assert len(assets) == 2
    assert assets[0].content_hash == assets[1].content_hash
    assert assets[0].preview_oss_path == assets[1].preview_oss_path
    assert assets[0].oss_path != assets[1].oss_path
    assert list(assets[0].vector) == list(assets[1].vector)
    assert embedding.calls == 1


def test_delete_product_image_archives_asset_without_deleting_oss_objects(app):
    storage, _embedding = _install_asset_dependencies(app)
    client = app.test_client()
    response = client.post('/api/products', data={
        'product': _product_payload('CS-ASSET-001'),
        'images': [(io.BytesIO(_png_bytes('red')), 'one.png')],
    }, content_type='multipart/form-data')
    assert response.status_code == 201
    asset = ImageAsset.query.one()
    object_keys = set(storage.objects)
    assert client.get('/api/products/statistics').get_json()['total_images'] == 1

    deleted = client.delete(
        f'/api/products/CS-ASSET-001/images/{asset.id}'
    )

    assert deleted.status_code == 200
    assert deleted.get_json()['message'] == '图片已归档'
    detail = client.get('/api/products/CS-ASSET-001').get_json()
    assert detail['images'] == []
    statistics = client.get('/api/products/statistics').get_json()
    assert statistics['total_images'] == 0

    db.session.refresh(asset)
    assert asset.status == 'archived'
    assert asset.archived_at is not None
    assert set(storage.objects) == object_keys


def test_same_content_twice_creates_distinct_assets_and_reuses_vector(app):
    storage, embedding = _install_asset_dependencies(app)
    client = app.test_client()
    data = _png_bytes('red')

    response = client.post('/api/products', data={
        'product': _product_payload('CS-001'),
        'images': [(io.BytesIO(data), '1.png'), (io.BytesIO(data), '副本.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 201
    body = response.get_json()
    assert body['uploaded_images'] == 2
    assert body['skipped_duplicates'] == []

    assets = ImageAsset.query.order_by(ImageAsset.created_at).all()
    assert len(assets) == 2
    assert assets[0].id != assets[1].id
    assert assets[0].content_hash == assets[1].content_hash
    assert assets[0].preview_oss_path == assets[1].preview_oss_path
    assert assets[0].oss_path != assets[1].oss_path
    assert embedding.calls == 1
    assert len(storage.objects) == 3


def test_update_failure_rolls_back_product_fields_and_all_new_assets(app):
    embedding = FakeAssetEmbedding(fail_on_call=2)
    _install_asset_dependencies(app, embedding=embedding)
    client = app.test_client()
    client.post('/api/products', data={'product': _product_payload('CS-001')},
                content_type='multipart/form-data')

    response = client.put('/api/products/CS-001', data={
        'product': json.dumps({'photographer_file': 'changed'}),
        'images': [
            (io.BytesIO(_png_bytes('red')), '1.png'),
            (io.BytesIO(_png_bytes('blue')), '2.png'),
        ],
    }, content_type='multipart/form-data')

    assert response.status_code == 503
    assert response.get_json() == {
        'error': '图片识别服务暂不可用，请稍后重试',
        'error_code': 'EMBEDDING_SERVICE_ERROR',
    }
    assert 'secret upstream failure' not in response.get_data(as_text=True)

    detail = client.get('/api/products/CS-001').get_json()
    assert detail['photographer_file'] == 'p'
    assert detail['images'] == []
    assert ImageAsset.query.count() == 0
def test_invalid_image_rejects_product_without_asset(app):
    _install_asset_dependencies(app)
    client = app.test_client()

    response = client.post('/api/products', data={
        'product': _product_payload('BROKEN-001'),
        'images': [(io.BytesIO(b'not an image'), 'broken.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 400
    assert response.get_json()['error_code'] == 'INVALID_IMAGE'
    assert client.get('/api/products/BROKEN-001').status_code == 404
    assert ImageAsset.query.count() == 0


def test_delete_product_detaches_active_asset_without_deleting_oss(app):
    storage, _embedding = _install_asset_dependencies(app)
    client = app.test_client()
    created = client.post('/api/products', data={
        'product': _product_payload('CS-001'),
        'images': [(io.BytesIO(_png_bytes('red')), 'one.png')],
    }, content_type='multipart/form-data')
    assert created.status_code == 201
    asset_id = ImageAsset.query.one().id
    object_keys = set(storage.objects)

    response = client.delete('/api/products/CS-001')
    assert response.status_code == 200
    assert client.get('/api/products/CS-001').status_code == 404

    db.session.expire_all()
    asset = db.session.get(ImageAsset, asset_id)
    assert asset is not None
    assert asset.model_number is None
    assert asset.status == 'active'
    assert set(storage.objects) == object_keys


def test_delete_product_requires_compatibility_migration_when_legacy_audit_is_nonempty(
    app,
    monkeypatch,
):
    client = app.test_client()
    created = client.post('/api/products', data={
        'product': _product_payload('LEGACY-AUDIT-001'),
    }, content_type='multipart/form-data')
    assert created.status_code == 201

    monkeypatch.setattr(
        'services.legacy_product_images.audit_legacy_product_images',
        lambda _connection: LegacyProductImagesAudit(True, 1),
    )
    response = client.delete('/api/products/LEGACY-AUDIT-001')

    assert response.status_code == 409
    assert response.get_json() == {
        'error': '检测到旧商品图片数据，请先制定兼容迁移方案后再删除商品',
        'error_code': 'LEGACY_PRODUCT_IMAGES_REQUIRE_MIGRATION',
    }
    assert client.get('/api/products/LEGACY-AUDIT-001').status_code == 200


def test_batch_delete_requires_compatibility_migration_when_legacy_audit_is_nonempty(
    app,
    monkeypatch,
):
    client = app.test_client()
    for model_number in ('LEGACY-AUDIT-002', 'LEGACY-AUDIT-003'):
        created = client.post('/api/products', data={
            'product': _product_payload(model_number),
        }, content_type='multipart/form-data')
        assert created.status_code == 201

    monkeypatch.setattr(
        'services.legacy_product_images.audit_legacy_product_images',
        lambda _connection: LegacyProductImagesAudit(True, 1),
    )
    response = client.post(
        '/api/products/batch-delete',
        json={'model_numbers': ['LEGACY-AUDIT-002', 'LEGACY-AUDIT-003']},
    )

    assert response.status_code == 409
    assert response.get_json() == {
        'error': '检测到旧商品图片数据，请先制定兼容迁移方案后再删除商品',
        'error_code': 'LEGACY_PRODUCT_IMAGES_REQUIRE_MIGRATION',
    }
    assert client.get('/api/products/LEGACY-AUDIT-002').status_code == 200
    assert client.get('/api/products/LEGACY-AUDIT-003').status_code == 200


def test_recreating_product_relinks_existing_upload_without_new_oss_objects(app):
    storage, embedding = _install_asset_dependencies(app)
    client = app.test_client()
    image = _png_bytes('red')

    created = client.post('/api/products', data={
        'product': _product_payload('RELINK-001'),
        'images': [(io.BytesIO(image), 'one.png')],
    }, content_type='multipart/form-data')
    assert created.status_code == 201
    asset_id = ImageAsset.query.one().id
    object_keys = set(storage.objects)
    upload_count = len(storage.uploaded_keys)
    embedding_calls = embedding.calls

    assert client.delete('/api/products/RELINK-001').status_code == 200
    db.session.expire_all()
    assert db.session.get(ImageAsset, asset_id).model_number is None

    recreated = client.post('/api/products', data={
        'product': _product_payload('RELINK-001'),
        'images': [(io.BytesIO(image), 'one.png')],
    }, content_type='multipart/form-data')

    assert recreated.status_code == 201
    recreated_body = recreated.get_json()
    assert recreated_body['uploaded_images'] == 0
    assert recreated_body['reused_images'] == 1
    assert recreated_body['skipped_duplicates'] == [str(asset_id)]
    assert recreated_body['image_results'] == [{
        'asset_id': str(asset_id),
        'source_relative_path': ImageAsset.query.one().source_relative_path,
        'status': 'existing',
    }]
    db.session.expire_all()
    asset = db.session.get(ImageAsset, asset_id)
    assert asset.model_number == 'RELINK-001'
    assert asset.status == 'active'
    assert set(storage.objects) == object_keys
    assert len(storage.uploaded_keys) == upload_count
    assert embedding.calls == embedding_calls


def test_reuploading_archived_product_image_returns_recycle_bin_without_restore(
    app,
):
    storage, embedding = _install_asset_dependencies(app)
    client = app.test_client()
    image = _png_bytes('blue')
    created = client.post('/api/products', data={
        'product': _product_payload('REACTIVATE-001'),
        'images': [(io.BytesIO(image), 'one.png')],
    }, content_type='multipart/form-data')
    assert created.status_code == 201
    asset_id = ImageAsset.query.one().id
    object_keys = set(storage.objects)
    upload_count = len(storage.uploaded_keys)
    embedding_calls = embedding.calls

    archived = client.delete(
        f'/api/products/REACTIVATE-001/images/{asset_id}'
    )
    assert archived.status_code == 200

    reuploaded = client.put('/api/products/REACTIVATE-001', data={
        'product': json.dumps({'photographer_file': 'changed'}),
        'images': [(io.BytesIO(image), 'one.png')],
    }, content_type='multipart/form-data')

    assert reuploaded.status_code == 200
    reuploaded_body = reuploaded.get_json()
    assert reuploaded_body['uploaded_images'] == 0
    assert reuploaded_body['reused_images'] == 0
    assert reuploaded_body['recycle_bin_images'] == 1
    assert reuploaded_body['skipped_duplicates'] == []
    assert reuploaded_body['image_results'] == [{
        'asset_id': str(asset_id),
        'source_relative_path': ImageAsset.query.one().source_relative_path,
        'status': 'in_recycle_bin',
        'recovery_action': {
            'type': 'open_recycle_bin',
            'asset_id': str(asset_id),
        },
    }]
    db.session.expire_all()
    asset = db.session.get(ImageAsset, asset_id)
    assert asset.status == 'archived'
    assert asset.archived_at is not None
    assert asset.model_number == 'REACTIVATE-001'
    assert set(storage.objects) == object_keys
    assert len(storage.uploaded_keys) == upload_count
    assert embedding.calls == embedding_calls


def test_batch_delete_detaches_all_active_assets(app):
    storage, _embedding = _install_asset_dependencies(app)
    client = app.test_client()
    for model_number, color in (('CS-001', 'red'), ('CS-002', 'blue')):
        response = client.post('/api/products', data={
            'product': _product_payload(model_number),
            'images': [(io.BytesIO(_png_bytes(color)), f'{color}.png')],
        }, content_type='multipart/form-data')
        assert response.status_code == 201
    object_keys = set(storage.objects)

    response = client.post(
        '/api/products/batch-delete',
        json={'model_numbers': ['CS-001', 'CS-002']},
    )

    assert response.status_code == 200
    assert response.get_json()['deleted_count'] == 2
    db.session.expire_all()
    assert {
        asset.model_number for asset in ImageAsset.query.all()
    } == {None}
    assert set(storage.objects) == object_keys


def test_storage_error_returns_stable_message_and_rolls_back_product(app):
    _install_asset_dependencies(app, storage=FailingAssetStorage())
    client = app.test_client()

    response = client.post('/api/products', data={
        'product': _product_payload('OSS-FAIL-001'),
        'images': [(io.BytesIO(_png_bytes('red')), 'one.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 503
    assert response.get_json() == {
        'error': '图片存储服务暂不可用，请稍后重试',
        'error_code': 'OBJECT_STORAGE_ERROR',
    }
    assert 'secret storage failure' not in response.get_data(as_text=True)
    assert client.get('/api/products/OSS-FAIL-001').status_code == 404
    assert ImageAsset.query.count() == 0


def test_storage_conflict_returns_409_without_overwriting_or_partial_row(app):
    storage = ConflictingAssetStorage()
    _install_asset_dependencies(app, storage=storage)
    client = app.test_client()

    response = client.post('/api/products', data={
        'product': _product_payload('OSS-CONFLICT-001'),
        'images': [(io.BytesIO(_png_bytes('red')), 'one.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 409
    assert response.get_json() == {
        'error': '图片资产发生冲突，未覆盖现有内容',
        'error_code': 'IMAGE_ASSET_CONFLICT',
    }
    assert storage.objects == {}
    assert client.get('/api/products/OSS-CONFLICT-001').status_code == 404
    assert ImageAsset.query.count() == 0


def test_failed_multi_image_request_can_retry_without_new_oss_orphans(app):
    storage = FakeAssetStorage()
    embedding = FakeAssetEmbedding(fail_on_call=2)
    _install_asset_dependencies(
        app,
        storage=storage,
        embedding=embedding,
    )
    client = app.test_client()
    first_image = _png_bytes('red')
    second_image = _png_bytes('blue')

    failed = client.post('/api/products', data={
        'product': _product_payload('RETRY-001'),
        'images': [
            (io.BytesIO(first_image), '主图.png'),
            (io.BytesIO(second_image), '细节图.png'),
        ],
    }, content_type='multipart/form-data')

    assert failed.status_code == 503
    assert ImageAsset.query.count() == 0
    preserved_keys = set(storage.objects)
    upload_count = len(storage.uploaded_keys)
    assert len(preserved_keys) == 4

    retried = client.post('/api/products', data={
        'product': _product_payload('RETRY-001'),
        'images': [
            (io.BytesIO(first_image), '主图.png'),
            (io.BytesIO(second_image), '细节图.png'),
        ],
    }, content_type='multipart/form-data')

    assert retried.status_code == 201
    assert retried.get_json()['uploaded_images'] == 2
    assert set(storage.objects) == preserved_keys
    assert len(storage.uploaded_keys) == upload_count
    assets = ImageAsset.query.order_by(ImageAsset.source_relative_path).all()
    assert len(assets) == 2
    assert {asset.oss_path for asset in assets}.issubset(preserved_keys)
    assert all(
        asset.source_relative_path.startswith('models/RETRY-001/')
        for asset in assets
    )


def test_commit_failure_can_retry_using_the_same_oss_source_identity(
    app,
    monkeypatch,
):
    storage, _embedding = _install_asset_dependencies(app)
    client = app.test_client()
    image = _png_bytes('green')
    real_commit = db.session.commit
    calls = {'count': 0}

    def fail_first_commit():
        calls['count'] += 1
        if calls['count'] == 1:
            raise RuntimeError('secret commit failure')
        return real_commit()

    monkeypatch.setattr(db.session, 'commit', fail_first_commit)
    failed = client.post('/api/products', data={
        'product': _product_payload('RETRY-COMMIT-001'),
        'images': [(io.BytesIO(image), 'one.png')],
    }, content_type='multipart/form-data')

    assert failed.status_code == 500
    assert 'secret commit failure' not in failed.get_data(as_text=True)
    assert ImageAsset.query.count() == 0
    preserved_keys = set(storage.objects)
    upload_count = len(storage.uploaded_keys)
    assert len(preserved_keys) == 2

    retried = client.post('/api/products', data={
        'product': _product_payload('RETRY-COMMIT-001'),
        'images': [(io.BytesIO(image), 'one.png')],
    }, content_type='multipart/form-data')

    assert retried.status_code == 201
    assert set(storage.objects) == preserved_keys
    assert len(storage.uploaded_keys) == upload_count
    assert ImageAsset.query.one().oss_path in preserved_keys


def test_delete_product_failure_returns_stable_error_without_leaking_details(
    app,
    monkeypatch,
    caplog,
):
    client = app.test_client()
    created = client.post('/api/products', data={
        'product': _product_payload('DELETE-FAIL-001'),
    }, content_type='multipart/form-data')
    assert created.status_code == 201

    def fail_commit():
        raise RuntimeError('secret delete failure')

    monkeypatch.setattr(db.session, 'commit', fail_commit)
    response = client.delete('/api/products/DELETE-FAIL-001')

    assert response.status_code == 500
    assert response.get_json() == {
        'error': '产品删除失败，请稍后重试',
        'error_code': 'PRODUCT_DELETE_FAILED',
    }
    assert 'secret delete failure' not in response.get_data(as_text=True)
    assert 'secret delete failure' not in caplog.text
    assert 'error_type=RuntimeError' in caplog.text


def test_batch_delete_failure_returns_stable_error_without_leaking_details(
    app,
    monkeypatch,
    caplog,
):
    client = app.test_client()
    created = client.post('/api/products', data={
        'product': _product_payload('BATCH-FAIL-001'),
    }, content_type='multipart/form-data')
    assert created.status_code == 201

    def fail_commit():
        raise RuntimeError('secret batch delete failure')

    monkeypatch.setattr(db.session, 'commit', fail_commit)
    response = client.post(
        '/api/products/batch-delete',
        json={'model_numbers': ['BATCH-FAIL-001']},
    )

    assert response.status_code == 500
    assert response.get_json() == {
        'error': '批量删除产品失败，请稍后重试',
        'error_code': 'PRODUCT_BATCH_DELETE_FAILED',
    }
    assert 'secret batch delete failure' not in response.get_data(as_text=True)
    assert 'secret batch delete failure' not in caplog.text
    assert 'error_type=RuntimeError' in caplog.text


def _upload_product_with_colors(client, model_number, colors):
    files = [
        (io.BytesIO(_png_bytes(color)), f'{color}.png') for color in colors
    ]
    return client.post('/api/products', data={
        'product': _product_payload(model_number),
        'images': files,
    }, content_type='multipart/form-data')


def _image_names(images):
    return [
        image['source_relative_path'].rsplit('/', 1)[-1] for image in images
    ]


def test_create_product_persists_upload_order_with_first_as_primary(app):
    _install_asset_dependencies(app)
    client = app.test_client()
    created = _upload_product_with_colors(client, 'ORDER-001', ['red', 'green', 'blue'])
    assert created.status_code == 201

    images = client.get('/api/products/ORDER-001').get_json()['images']
    assert _image_names(images) == ['red.png', 'green.png', 'blue.png']
    assert [image['image_order'] for image in images] == [0, 1, 2]
    assert [image['is_primary'] for image in images] == [True, False, False]
    sort_orders = {
        asset.source_relative_path.rsplit('/', 1)[-1]: asset.sort_order
        for asset in ImageAsset.query.all()
    }
    assert sort_orders == {'red.png': 0, 'green.png': 1, 'blue.png': 2}


def test_update_product_applies_explicit_image_order(app):
    _install_asset_dependencies(app)
    client = app.test_client()
    assert _upload_product_with_colors(
        client, 'ORDER-002', ['red', 'green', 'blue']
    ).status_code == 201
    before = client.get('/api/products/ORDER-002').get_json()['images']
    reordered = [before[2]['asset_id'], before[0]['asset_id'], before[1]['asset_id']]

    response = client.put('/api/products/ORDER-002', data={
        'product': json.dumps({'image_order': reordered}),
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    after = client.get('/api/products/ORDER-002').get_json()['images']
    assert [image['asset_id'] for image in after] == reordered
    assert _image_names(after) == ['blue.png', 'red.png', 'green.png']
    assert [image['is_primary'] for image in after] == [True, False, False]


def test_update_product_image_order_places_new_upload_at_placeholder(app):
    _install_asset_dependencies(app)
    client = app.test_client()
    assert _upload_product_with_colors(
        client, 'ORDER-003', ['red', 'blue']
    ).status_code == 201
    before = client.get('/api/products/ORDER-003').get_json()['images']

    response = client.put('/api/products/ORDER-003', data={
        'product': json.dumps({'image_order': [
            before[0]['asset_id'], 'new:0', before[1]['asset_id'],
        ]}),
        'images': [(io.BytesIO(_png_bytes('green')), 'green.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    after = client.get('/api/products/ORDER-003').get_json()['images']
    assert _image_names(after) == ['red.png', 'green.png', 'blue.png']


def test_update_product_appends_new_uploads_without_image_order(app):
    _install_asset_dependencies(app)
    client = app.test_client()
    assert _upload_product_with_colors(
        client, 'ORDER-004', ['red', 'green']
    ).status_code == 201

    response = client.put('/api/products/ORDER-004', data={
        'product': json.dumps({'photographer_file': 'changed'}),
        'images': [(io.BytesIO(_png_bytes('blue')), 'blue.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    after = client.get('/api/products/ORDER-004').get_json()['images']
    assert _image_names(after) == ['red.png', 'green.png', 'blue.png']


def test_update_product_rejects_invalid_image_order(app):
    _install_asset_dependencies(app)
    client = app.test_client()
    assert _upload_product_with_colors(client, 'ORDER-005', ['red']).status_code == 201

    for bad_payload in ('not-a-list', [123]):
        response = client.put('/api/products/ORDER-005', data={
            'product': json.dumps({'image_order': bad_payload}),
        }, content_type='multipart/form-data')
        assert response.status_code == 400
        assert response.get_json()['error_code'] == 'INVALID_IMAGE_ORDER'
    images = client.get('/api/products/ORDER-005').get_json()['images']
    assert _image_names(images) == ['red.png']


def test_update_product_image_order_skips_unknown_and_foreign_assets(app):
    _install_asset_dependencies(app)
    client = app.test_client()
    assert _upload_product_with_colors(
        client, 'ORDER-006', ['red', 'green']
    ).status_code == 201
    assert _upload_product_with_colors(
        client, 'ORDER-007', ['blue']
    ).status_code == 201
    own = client.get('/api/products/ORDER-006').get_json()['images']
    foreign = client.get('/api/products/ORDER-007').get_json()['images'][0]

    response = client.put('/api/products/ORDER-006', data={
        'product': json.dumps({'image_order': [
            own[1]['asset_id'],
            '00000000-0000-0000-0000-000000000000',
            foreign['asset_id'],
        ]}),
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    after = client.get('/api/products/ORDER-006').get_json()['images']
    assert _image_names(after) == ['green.png', 'red.png']
    assert _image_names(
        client.get('/api/products/ORDER-007').get_json()['images']
    ) == ['blue.png']


def test_assign_appends_assets_after_existing_images(app):
    _install_asset_dependencies(app)
    client = app.test_client()
    assert _upload_product_with_colors(client, 'ASSIGN-001', ['red']).status_code == 201
    primary_id = client.get('/api/products/ASSIGN-001').get_json()['images'][0]['asset_id']
    assert _upload_product_with_colors(
        client, 'ASSIGN-TMP', ['green', 'blue']
    ).status_code == 201
    tmp_images = client.get('/api/products/ASSIGN-TMP').get_json()['images']
    tmp_ids = [image['asset_id'] for image in tmp_images]
    assert client.delete('/api/products/ASSIGN-TMP').status_code == 200

    response = client.post('/api/image-assets/assign', json={
        'asset_ids': tmp_ids,
        'model_number': 'ASSIGN-001',
    })

    assert response.status_code == 200
    images = client.get('/api/products/ASSIGN-001').get_json()['images']
    assert [image['asset_id'] for image in images] == [primary_id] + tmp_ids
    assert [image['is_primary'] for image in images] == [True, False, False]


def test_update_product_image_order_placeholder_stays_aligned_with_skipped_uploads(app):
    """被扩展名白名单静默跳过的上传不占据 new:<index> 槽位。"""
    _install_asset_dependencies(app)
    client = app.test_client()
    assert _upload_product_with_colors(client, 'ORDER-008', ['red']).status_code == 201
    before = client.get('/api/products/ORDER-008').get_json()['images']

    response = client.put('/api/products/ORDER-008', data={
        'product': json.dumps({'image_order': [
            'new:1', before[0]['asset_id'],
        ]}),
        'images': [
            (io.BytesIO(b'not a real image'), 'photo.heic'),
            (io.BytesIO(_png_bytes('green')), 'green.png'),
        ],
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    after = client.get('/api/products/ORDER-008').get_json()['images']
    assert _image_names(after) == ['green.png', 'red.png']
