"""本地导入（单图/文件夹/剪贴板）进入待归款图片的集成契约。"""
import hashlib
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from PIL import Image
from sqlalchemy import text

from models import ImageAsset, Product, db
from services.image_normalizer import ImageNormalizer
from services.object_storage import (
    ObjectStorageConflictError,
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


class FakeImportStorage:
    def __init__(self):
        self.objects: dict[str, _FakeStoredObject] = {}
        self.put_calls: list[str] = []
        self._lock = threading.RLock()

    def head_object(self, key):
        with self._lock:
            item = self.objects.get(key)
            if item is None:
                return None
            return StoredObject(
                key=key,
                size=len(item.data),
                content_type=item.content_type,
                metadata=dict(item.metadata),
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
        with self._lock:
            if key in self.objects:
                raise ObjectStorageConflictError(
                    f'put-if-absent conflict for {key}'
                )
            payload = bytes(data)
            assert hashlib.md5(
                payload,
                usedforsecurity=False,
            ).hexdigest() == spec.md5_hex
            self.objects[key] = _FakeStoredObject(
                data=payload,
                content_type=spec.content_type,
                metadata=dict(spec.metadata),
                etag=spec.md5_hex,
            )
            self.put_calls.append(key)


class FakeImportEmbedding:
    def __init__(self):
        self.batch_calls = []
        self._batch_calls_lock = threading.Lock()

    def embed_normalized_image(self, image_path, request_id=None):
        return np.full(1024, 0.1, dtype=np.float32)

    def embed_normalized_images(self, image_paths, request_id=None):
        with self._batch_calls_lock:
            self.batch_calls.append(len(image_paths))
        return [
            self.embed_normalized_image(path, request_id=request_id)
            for path in image_paths
        ]


def _install_import_dependencies(app):
    storage = FakeImportStorage()
    embedding = FakeImportEmbedding()
    app.config['IMAGE_ASSET_STORAGE'] = storage
    app.config['IMAGE_INGEST_EMBEDDING'] = embedding
    return storage, embedding


def _import_request(client, entries, prefix='手动导入'):
    """entries: [(bytes, upload_filename, relative_path), ...]"""
    data = {
        'images': [
            (io.BytesIO(payload), filename)
            for payload, filename, _ in entries
        ],
        'relative_paths': json.dumps([path for _, _, path in entries]),
        'prefix': prefix,
    }
    return client.post(
        '/api/image-assets/import',
        data=data,
        content_type='multipart/form-data',
    )


def _parallel_import_requests(app, entries):
    start = threading.Barrier(2)
    pids = []
    pid_lock = threading.Lock()

    def post_one(entry):
        with app.app_context():
            try:
                pid = db.session.execute(
                    text('SELECT pg_backend_pid()')
                ).scalar_one()
                with pid_lock:
                    pids.append(pid)
                start.wait(timeout=10)
                return _import_request(app.test_client(), [entry])
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(post_one, entries))
    assert len(set(pids)) == 2
    return responses


class BarrierImportEmbedding(FakeImportEmbedding):
    def __init__(self):
        super().__init__()
        self.barrier = threading.Barrier(2)
        self.pids = []
        self.barrier_waits = 0
        self._pid_lock = threading.Lock()

    def embed_normalized_images(self, image_paths, request_id=None):
        pid = db.session.execute(text('SELECT pg_backend_pid()')).scalar_one()
        with self._pid_lock:
            self.pids.append(pid)
        vectors = super().embed_normalized_images(
            image_paths,
            request_id=request_id,
        )
        self.barrier.wait(timeout=10)
        with self._pid_lock:
            self.barrier_waits += 1
        return vectors


class BarrierImportNormalizer:
    """在来源身份查询前让两个真实 HTTP handler 确定性交叠。"""

    def __init__(self):
        self._delegate = ImageNormalizer()
        self._barrier = threading.Barrier(2)
        self._lock = threading.Lock()
        self._gated_calls = 0
        self.handler_pids = []
        self.barrier_waits = 0

    @property
    def normalization_version(self):
        # _prepare_one 先以该版本构造 preview key，之后才查询来源身份。
        pid = db.session.execute(text('SELECT pg_backend_pid()')).scalar_one()
        with self._lock:
            should_wait = self._gated_calls < 2
            self._gated_calls += 1
            if should_wait:
                self.handler_pids.append(pid)
        if should_wait:
            self._barrier.wait(timeout=10)
            with self._lock:
                self.barrier_waits += 1
        return self._delegate.normalization_version

    def normalize(self, source_path):
        return self._delegate.normalize(source_path)


def _assert_count_partition(body):
    assert (
        body['created_count']
        + body['existing_count']
        + body['conflict_count']
        + body['recycle_bin_count']
        + body['failed_count']
    ) == len(body['items'])
    assert body['skipped_count'] == 0


def test_import_creates_unassigned_asset_without_product(app):
    storage, _embedding = _install_import_dependencies(app)
    client = app.test_client()
    original = _png_bytes('red')

    response = _import_request(client, [
        (original, '2.png', '手机挂绳/A47/修改后/2.png'),
    ])

    assert response.status_code == 200
    body = response.get_json()
    assert body['created_count'] == 1
    assert body['existing_count'] == 0
    assert body['conflict_count'] == 0
    assert body['recycle_bin_count'] == 0
    assert body['skipped_count'] == 0
    assert body['failed_count'] == 0
    item = body['items'][0]
    assert item == {
        'relative_path': '手动导入/手机挂绳/A47/修改后/2.png',
        'status': 'created',
        'asset_id': item['asset_id'],
        'error': None,
        'recovery_action': None,
    }

    asset = ImageAsset.query.one()
    assert asset.model_number is None
    assert asset.status == 'active'
    assert asset.source_provider == 'local-import'
    assert asset.source_bucket == 'user-imports'
    # 嵌套相对路径原样保留，展示形式与现有资产一致。
    assert asset.source_relative_path == '手动导入/手机挂绳/A47/修改后/2.png'
    assert storage.objects[asset.oss_path].data == original

    # 不创建产品记录。
    assert Product.query.count() == 0

    # 自动出现在待归款列表并可走私有预览 302。
    listed = client.get('/api/image-assets?assignment=unassigned')
    assert listed.get_json()['total'] == 1
    listed_item = listed.get_json()['assets'][0]
    assert listed_item['source_relative_path'] == (
        '手动导入/手机挂绳/A47/修改后/2.png'
    )
    preview = client.get(listed_item['preview_url'])
    assert preview.status_code == 302
    assert preview.headers['Location'].startswith('https://private.example/')


def test_import_same_content_at_different_paths_creates_distinct_assets(app):
    storage, embedding = _install_import_dependencies(app)
    client = app.test_client()
    original = _png_bytes('red')

    first = _import_request(client, [(original, 'a.png', 'a.png')])
    second = _import_request(client, [
        (original, 'b.png', 'b.png'),
        (_png_bytes('blue'), 'c.png', 'c.png'),
    ])

    assert first.status_code == 200
    assert second.status_code == 200
    body = second.get_json()
    assert [item['status'] for item in body['items']] == ['created', 'created']
    assert body['created_count'] == 2
    assert body['existing_count'] == 0
    assert body['conflict_count'] == 0
    assert body['recycle_bin_count'] == 0
    assert body['failed_count'] == 0
    assert body['skipped_count'] == 0
    rows = ImageAsset.query.order_by(ImageAsset.source_relative_path).all()
    assert len(rows) == 3
    same_content = [row for row in rows if row.content_hash == rows[0].content_hash]
    assert len(same_content) == 2
    assert same_content[0].id != same_content[1].id
    assert same_content[0].preview_oss_path == same_content[1].preview_oss_path
    assert list(same_content[0].vector) == list(same_content[1].vector)
    assert sum(embedding.batch_calls) == 2
    assert storage.put_calls.count(same_content[0].preview_oss_path) == 1


def test_import_same_content_at_different_paths_in_one_batch_creates_distinct_assets(app):
    storage, embedding = _install_import_dependencies(app)
    client = app.test_client()
    original = _png_bytes('red')

    response = _import_request(client, [
        (original, 'a.png', 'a.png'),
        (original, 'a-copy.png', 'nested/a-copy.png'),
    ])

    assert response.status_code == 200
    body = response.get_json()
    assert [item['relative_path'] for item in body['items']] == [
        '手动导入/a.png',
        '手动导入/nested/a-copy.png',
    ]
    assert [item['status'] for item in body['items']] == ['created', 'created']
    assert body['created_count'] == 2
    assert body['skipped_count'] == 0
    rows = ImageAsset.query.order_by(ImageAsset.source_relative_path).all()
    assert len(rows) == 2
    assert rows[0].content_hash == rows[1].content_hash
    assert rows[0].preview_oss_path == rows[1].preview_oss_path
    assert list(rows[0].vector) == list(rows[1].vector)
    assert sum(embedding.batch_calls) == 1
    assert storage.put_calls.count(rows[0].preview_oss_path) == 1


def test_import_same_path_different_content_reports_source_conflict(app):
    storage, _embedding = _install_import_dependencies(app)
    client = app.test_client()
    original = _png_bytes('red')
    created = _import_request(client, [(original, 'a.png', 'a.png')])
    existing_id = created.get_json()['items'][0]['asset_id']
    original_key = ImageAsset.query.one().oss_path
    before = storage.objects[original_key]

    response = _import_request(client, [
        (_png_bytes('blue'), 'a.png', 'a.png'),
    ])

    body = response.get_json()
    assert body['items'][0] == {
        'relative_path': '手动导入/a.png',
        'status': 'source_conflict',
        'asset_id': existing_id,
        'error': '来源冲突：同一路径已存在不同内容的图片',
        'recovery_action': None,
    }
    assert body['conflict_count'] == 1
    assert body['failed_count'] == 0
    _assert_count_partition(body)
    assert ImageAsset.query.count() == 1
    assert storage.objects[original_key] == before


def test_import_same_path_same_content_is_safe_to_retry(app):
    storage, embedding = _install_import_dependencies(app)
    client = app.test_client()
    original = _png_bytes('red')

    first = _import_request(client, [(original, 'a.png', 'a.png')])
    second = _import_request(client, [(original, 'a.png', 'a.png')])

    created = first.get_json()['items'][0]
    body = second.get_json()
    item = body['items'][0]
    assert item == {
        'relative_path': '手动导入/a.png',
        'status': 'existing',
        'asset_id': created['asset_id'],
        'error': None,
        'recovery_action': None,
    }
    assert body['existing_count'] == 1
    _assert_count_partition(body)
    assert ImageAsset.query.count() == 1
    assert sum(embedding.batch_calls) == 1
    assert storage.objects[ImageAsset.query.one().oss_path].data == original


def test_import_archived_same_source_returns_recycle_bin_result(app):
    storage, embedding = _install_import_dependencies(app)
    client = app.test_client()
    original = _png_bytes('red')
    created = _import_request(client, [(original, 'a.png', 'a.png')])
    asset = ImageAsset.query.one()
    asset.status = 'archived'
    asset.archived_at = datetime.now()
    archived_at = asset.archived_at
    db.session.commit()
    put_count = len(storage.put_calls)

    response = _import_request(client, [(original, 'a.png', 'a.png')])

    body = response.get_json()
    asset_id = created.get_json()['items'][0]['asset_id']
    assert body['items'][0] == {
        'relative_path': '手动导入/a.png',
        'status': 'in_recycle_bin',
        'asset_id': asset_id,
        'error': None,
        'recovery_action': {
            'type': 'open_recycle_bin',
            'asset_id': asset_id,
        },
    }
    assert body['recycle_bin_count'] == 1
    _assert_count_partition(body)
    db.session.expire_all()
    unchanged = ImageAsset.query.one()
    assert unchanged.status == 'archived'
    assert unchanged.archived_at == archived_at
    assert len(storage.put_calls) == put_count
    assert sum(embedding.batch_calls) == 1


def test_import_reuses_matching_orphan_objects_without_overwrite(app):
    storage, embedding = _install_import_dependencies(app)
    client = app.test_client()
    original = _png_bytes('orange')
    original_embed = embedding.embed_normalized_images
    embedding.embed_normalized_images = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError('temporary embedding failure')
    )

    failed = _import_request(client, [(original, 'orphan.png', 'orphan.png')])
    assert failed.get_json()['items'][0]['status'] == 'failed'
    assert ImageAsset.query.count() == 0
    stored_before_retry = dict(storage.objects)
    put_count = len(storage.put_calls)

    embedding.embed_normalized_images = original_embed
    retried = _import_request(client, [(original, 'orphan.png', 'orphan.png')])

    body = retried.get_json()
    assert body['items'][0]['status'] == 'created'
    assert body['created_count'] == 1
    _assert_count_partition(body)
    assert ImageAsset.query.count() == 1
    assert storage.objects == stored_before_retry
    assert len(storage.put_calls) == put_count


def test_import_reports_embedding_failure_as_failed_item(app):
    _storage, embedding = _install_import_dependencies(app)
    client = app.test_client()
    embedding.embed_normalized_images = lambda *args, **kwargs: [None]

    response = _import_request(client, [
        (_png_bytes('red'), 'failed.png', 'failed.png'),
    ])

    body = response.get_json()
    assert body['items'][0]['status'] == 'failed'
    assert body['items'][0]['asset_id'] is None
    assert body['items'][0]['recovery_action'] is None
    assert body['failed_count'] == 1
    assert body['conflict_count'] == 0
    _assert_count_partition(body)
    assert ImageAsset.query.count() == 0


def test_import_rejects_invalid_paths_and_extensions(app):
    _install_import_dependencies(app)
    client = app.test_client()
    payload = _png_bytes('red')

    traversal = _import_request(client, [(payload, 'a.png', '../escape.png')])
    absolute = _import_request(client, [(payload, 'a.png', '/abs/a.png')])
    bad_extension = _import_request(
        client, [(payload, 'a.txt', 'notes/a.txt')]
    )
    assert traversal.status_code == 400
    assert traversal.get_json()['error_code'] == 'INVALID_IMAGE_ASSET_IMPORT'
    assert absolute.status_code == 400
    assert bad_extension.status_code == 400
    assert ImageAsset.query.count() == 0


def test_import_rejects_oversized_batch_and_duplicate_paths(app):
    _install_import_dependencies(app)
    client = app.test_client()

    oversized = _import_request(client, [
        (_png_bytes('red'), f'{index}.png', f'{index}.png')
        for index in range(21)
    ])
    assert oversized.status_code == 400
    assert oversized.get_json()['error_code'] == 'INVALID_IMAGE_ASSET_IMPORT'

    duplicate_paths = _import_request(client, [
        (_png_bytes('red'), 'a.png', 'same/a.png'),
        (_png_bytes('blue'), 'b.png', 'same/a.png'),
    ])
    assert duplicate_paths.status_code == 400
    assert ImageAsset.query.count() == 0


def test_import_returns_json_413_before_any_write_when_body_is_too_large(app):
    storage, _embedding = _install_import_dependencies(app)
    client = app.test_client()
    app.config['MAX_CONTENT_LENGTH'] = 128

    response = _import_request(client, [
        (_png_bytes('red'), 'large.png', 'large.png'),
    ])

    assert response.status_code == 413
    assert response.get_json() == {
        'error': '上传图片过大',
        'error_code': 'IMAGE_TOO_LARGE',
    }
    assert storage.objects == {}
    assert ImageAsset.query.count() == 0


def test_concurrent_http_same_source_same_content_converges(concurrent_app):
    storage = FakeImportStorage()
    embedding = BarrierImportEmbedding()
    concurrent_app.config['IMAGE_ASSET_STORAGE'] = storage
    concurrent_app.config['IMAGE_INGEST_EMBEDDING'] = embedding
    original = _png_bytes('purple')
    entry = (original, 'same.png', 'same.png')

    responses = _parallel_import_requests(concurrent_app, [entry, entry])

    bodies = [response.get_json() for response in responses]
    assert [response.status_code for response in responses] == [200, 200]
    for body in bodies:
        _assert_count_partition(body)
    assert sorted(body['items'][0]['status'] for body in bodies) == [
        'created',
        'existing',
    ]
    assert len({body['items'][0]['asset_id'] for body in bodies}) == 1
    assert len(set(embedding.pids)) == 2
    assert embedding.barrier_waits == 2
    assert sum(embedding.batch_calls) == 2
    with concurrent_app.app_context():
        assert ImageAsset.query.count() == 1


def test_concurrent_http_existing_and_changed_content_are_deterministic(
    concurrent_app,
):
    storage, embedding = _install_import_dependencies(concurrent_app)
    original = _png_bytes('red')
    with concurrent_app.app_context():
        created = _import_request(
            concurrent_app.test_client(),
            [(original, 'same.png', 'same.png')],
        ).get_json()['items'][0]
        asset = ImageAsset.query.one()
        original_key = asset.oss_path
        before = storage.objects[original_key]
        db.session.remove()
    normalizer = BarrierImportNormalizer()
    concurrent_app.config['IMAGE_ASSET_NORMALIZER'] = normalizer

    responses = _parallel_import_requests(concurrent_app, [
        (original, 'same.png', 'same.png'),
        (_png_bytes('blue'), 'same.png', 'same.png'),
    ])

    items = [response.get_json()['items'][0] for response in responses]
    assert [response.status_code for response in responses] == [200, 200]
    for response in responses:
        _assert_count_partition(response.get_json())
    assert sorted(item['status'] for item in items) == [
        'existing',
        'source_conflict',
    ]
    assert {item['asset_id'] for item in items} == {created['asset_id']}
    assert len(set(normalizer.handler_pids)) == 2
    assert normalizer.barrier_waits == 2
    assert storage.objects[original_key] == before
    assert sum(embedding.batch_calls) == 1


def test_concurrent_http_archived_hits_never_restore(concurrent_app):
    storage, embedding = _install_import_dependencies(concurrent_app)
    original = _png_bytes('red')
    with concurrent_app.app_context():
        created = _import_request(
            concurrent_app.test_client(),
            [(original, 'same.png', 'same.png')],
        ).get_json()['items'][0]
        asset = ImageAsset.query.one()
        asset.status = 'archived'
        asset.archived_at = datetime.now()
        archived_at = asset.archived_at
        db.session.commit()
        put_count = len(storage.put_calls)
        db.session.remove()
    normalizer = BarrierImportNormalizer()
    concurrent_app.config['IMAGE_ASSET_NORMALIZER'] = normalizer

    responses = _parallel_import_requests(concurrent_app, [
        (original, 'same.png', 'same.png'),
        (original, 'same.png', 'same.png'),
    ])

    items = [response.get_json()['items'][0] for response in responses]
    assert [response.status_code for response in responses] == [200, 200]
    for response in responses:
        _assert_count_partition(response.get_json())
    assert [item['status'] for item in items] == [
        'in_recycle_bin',
        'in_recycle_bin',
    ]
    assert {item['asset_id'] for item in items} == {created['asset_id']}
    assert len(set(normalizer.handler_pids)) == 2
    assert normalizer.barrier_waits == 2
    with concurrent_app.app_context():
        unchanged = ImageAsset.query.one()
        assert unchanged.status == 'archived'
        assert unchanged.archived_at == archived_at
    assert len(storage.put_calls) == put_count
    assert sum(embedding.batch_calls) == 1
