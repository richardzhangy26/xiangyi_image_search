"""本地导入（单图/文件夹/剪贴板）进入待归款图片的集成契约。"""
import io
import json
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image

from models import ImageAsset, Product, db
from services.object_storage import SignedDownloadUrl, StoredObject


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
        self.objects[key] = _FakeStoredObject(
            data=bytes(data),
            content_type=spec.content_type,
            metadata=dict(spec.metadata),
            etag=spec.md5_hex,
        )


class FakeImportEmbedding:
    def __init__(self):
        self.batch_calls = []

    def embed_normalized_image(self, image_path, request_id=None):
        return np.full(1024, 0.1, dtype=np.float32)

    def embed_normalized_images(self, image_paths, request_id=None):
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
    assert body['skipped_count'] == 0
    assert body['failed_count'] == 0
    item = body['items'][0]
    assert item['status'] == 'created'
    assert item['relative_path'] == '手动导入/手机挂绳/A47/修改后/2.png'

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


def test_import_skips_content_duplicates(app):
    _install_import_dependencies(app)
    client = app.test_client()
    original = _png_bytes('red')

    first = _import_request(client, [(original, 'a.png', 'a.png')])
    second = _import_request(client, [
        (original, 'b.png', 'b.png'),
        (_png_bytes('blue'), 'c.png', 'c.png'),
    ])

    assert first.get_json()['created_count'] == 1
    body = second.get_json()
    assert body['created_count'] == 1
    assert body['skipped_count'] == 1
    statuses = {item['relative_path']: item['status'] for item in body['items']}
    assert statuses['手动导入/b.png'] == 'skipped_duplicate_content'
    assert statuses['手动导入/c.png'] == 'created'
    assert ImageAsset.query.count() == 2


def test_import_skips_in_batch_duplicate_content(app):
    _install_import_dependencies(app)
    client = app.test_client()
    original = _png_bytes('red')

    response = _import_request(client, [
        (original, 'a.png', 'a.png'),
        (original, 'a-copy.png', 'nested/a-copy.png'),
    ])

    body = response.get_json()
    assert body['created_count'] == 1
    assert body['skipped_count'] == 1
    assert body['items'][1]['status'] == 'skipped_duplicate_content'
    assert ImageAsset.query.count() == 1


def test_import_same_path_different_content_reports_conflict(app):
    _install_import_dependencies(app)
    client = app.test_client()

    first = _import_request(client, [
        (_png_bytes('red'), 'a.png', 'a.png'),
    ])
    second = _import_request(client, [
        (_png_bytes('blue'), 'a.png', 'a.png'),
    ])

    assert first.get_json()['created_count'] == 1
    body = second.get_json()
    assert body['failed_count'] == 1
    item = body['items'][0]
    assert item['status'] == 'source_conflict'
    assert '名字重复' in item['error']
    # 冲突不得覆盖或新增资产。
    assert ImageAsset.query.count() == 1


def test_import_same_path_same_content_is_safe_to_retry(app):
    storage, embedding = _install_import_dependencies(app)
    client = app.test_client()
    original = _png_bytes('red')

    first = _import_request(client, [(original, 'a.png', 'a.png')])
    second = _import_request(client, [(original, 'a.png', 'a.png')])

    assert first.get_json()['created_count'] == 1
    # 重试被内容预检拦截为跳过；不新增行，也不再调用 embedding。
    body = second.get_json()
    assert body['skipped_count'] == 1
    assert body['items'][0]['status'] == 'skipped_duplicate_content'
    assert ImageAsset.query.count() == 1
    assert sum(embedding.batch_calls) == 1
    asset = ImageAsset.query.one()
    assert storage.objects[asset.oss_path].data == original


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
