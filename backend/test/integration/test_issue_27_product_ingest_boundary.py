"""#27 生产工厂注入：Product/导入队列 HTTP 多图的 caller-owned 围栏生命周期。

启用 ``INGEST_BINDING_FENCE_ENABLED`` 后，/api/products 与 /api/image-imports 的
多图请求必须：成功时资产/任务与围栏释放随外层 commit 原子生效；任一失败时
外层 rollback 先于 abort（经独立 control session），产品/资产/任务行不提交、
围栏不留 held 残留。真实 PostgreSQL（concurrent_app 临时 schema）+ 伪 OSS。
"""

from __future__ import annotations

import hashlib
import io
import json
from contextlib import contextmanager
from dataclasses import dataclass

import numpy as np
from PIL import Image
from sqlalchemy.orm import sessionmaker

from models import ImageAsset, ImageImportItem, ObjectBindingFence, Product, db
from services.embedding import EMBEDDING_DIMENSION, EmbeddingServiceError
from services.object_storage import StoredObject


def _png_bytes(color='red', size=(40, 24)):
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, format='PNG')
    return output.getvalue()


@dataclass
class _FakeObject:
    data: bytes
    content_type: str
    metadata: dict[str, str]


class FakeOss:
    def __init__(self):
        self.objects = {}
        self.head_calls = []
        self.put_calls = []

    def head_object(self, key):
        self.head_calls.append(key)
        item = self.objects.get(key)
        if item is None:
            return None
        return StoredObject(
            key=key,
            size=len(item.data),
            content_type=item.content_type,
            metadata=item.metadata,
            etag=hashlib.md5(item.data, usedforsecurity=False).hexdigest(),
        )

    def put_file(self, key, source_path, *, spec):
        assert key not in self.objects
        with open(source_path, 'rb') as source:
            data = source.read()
        assert hashlib.md5(data, usedforsecurity=False).hexdigest() == spec.md5_hex
        self.objects[key] = _FakeObject(data, spec.content_type, dict(spec.metadata))
        self.put_calls.append(key)

    def put_bytes(self, key, data, *, spec):
        assert key not in self.objects
        assert hashlib.md5(data, usedforsecurity=False).hexdigest() == spec.md5_hex
        self.objects[key] = _FakeObject(data, spec.content_type, dict(spec.metadata))
        self.put_calls.append(key)


class FakeEmbedding:
    """请求级 chunk 入口经 ``embed_normalized_images`` 一次批量调用。

    ``fail_nth_representative``：第 N 个代表返回错维向量 → 服务抛
    AssetIngestError(stage=embedding)，端点映射 503。
    """

    def __init__(self, fail_nth_representative=None):
        self.batch_calls = []
        self.single_calls = 0
        self.fail_nth_representative = fail_nth_representative

    def embed_normalized_image(self, image_path, request_id=None):
        self.single_calls += 1
        if (
            self.fail_nth_representative is not None
            and self.single_calls == self.fail_nth_representative
        ):
            return np.zeros(5, dtype=np.float32)
        return np.full(EMBEDDING_DIMENSION, 0.1, dtype=np.float32)

    def embed_normalized_images(self, image_paths, request_id=None):
        self.batch_calls.append(len(image_paths))
        return [
            self.embed_normalized_image(path, request_id=request_id)
            for path in image_paths
        ]


def _fence_state_summary(observer):
    rows = observer.query(ObjectBindingFence).all()
    return {
        'total': len(rows),
        'held': sum(1 for row in rows if row.state == 'held'),
        'completed': sum(
            1 for row in rows
            if row.state == 'released' and row.release_reason == 'completed'
        ),
        'failed': sum(
            1 for row in rows
            if row.state == 'released' and row.release_reason == 'failed'
        ),
    }


@contextmanager
def _fenced_app(app):
    app.config['INGEST_BINDING_FENCE_ENABLED'] = True
    app.config['OSS_BUCKET_NAME'] = 'formal-test-bucket'
    storage = FakeOss()
    app.config['IMAGE_ASSET_STORAGE'] = storage
    observers = []

    def Session():
        session = sessionmaker(bind=db.engine)()
        observers.append(session)
        return session

    try:
        yield storage, Session
    finally:
        for session in observers:
            session.close()
        db.session.remove()


def _multipart_images(items):
    return [(io.BytesIO(data), name) for name, data in items]


def test_product_creation_releases_fences_atomically_on_commit(concurrent_app):
    with concurrent_app.app_context():
        concurrent_app.config['IMAGE_INGEST_EMBEDDING'] = FakeEmbedding()
        with _fenced_app(concurrent_app) as (storage, Session):
            client = concurrent_app.test_client()
            response = client.post(
                '/api/products',
                data={
                    'product': json.dumps({'model_number': 'FENCE-A'}),
                    'images': _multipart_images([
                        ('a.png', _png_bytes('red')),
                        ('b.png', _png_bytes('green')),
                    ]),
                },
                content_type='multipart/form-data',
            )

            assert response.status_code == 201, response.get_json()
            observer = Session()
            assert observer.query(Product).count() == 1
            assert observer.query(ImageAsset).count() == 2
            # 每个 item 的 finalize 释放都随外层 commit 原子生效。
            assert _fence_state_summary(observer) == {
                'total': 4, 'held': 0, 'completed': 4, 'failed': 0,
            }


def test_product_creation_rollback_after_second_image_releases_first_leases(concurrent_app):
    with concurrent_app.app_context():
        # 第二张图 embedding 失败 → 端点 503；第一张已 finalize 的租约
        # 必须先随外层 rollback 回到 held、再由边界经 control session 释放。
        concurrent_app.config['IMAGE_INGEST_EMBEDDING'] = FakeEmbedding(
            fail_nth_representative=2,
        )
        with _fenced_app(concurrent_app) as (storage, Session):
            client = concurrent_app.test_client()
            response = client.post(
                '/api/products',
                data={
                    'product': json.dumps({'model_number': 'FENCE-B'}),
                    'images': _multipart_images([
                        ('a.png', _png_bytes('red')),
                        ('b.png', _png_bytes('green')),
                    ]),
                },
                content_type='multipart/form-data',
            )

            assert response.status_code == 503
            observer = Session()
            assert observer.query(Product).count() == 0
            assert observer.query(ImageAsset).count() == 0
            # 整请求一个 chunk 租约（4 把围栏）在 finalize 前失败，由服务
            # 经 control session 整体释放。零 held 残留。
            assert _fence_state_summary(observer) == {
                'total': 4, 'held': 0, 'completed': 0, 'failed': 4,
            }
            # 已上传对象为重试保留，不删除。
            assert len(storage.put_calls) == 4


def test_import_queue_rollback_releases_first_item_leases(concurrent_app):
    with concurrent_app.app_context():
        with _fenced_app(concurrent_app) as (storage, Session):
            client = concurrent_app.test_client()
            response = client.post(
                '/api/image-imports',
                data={
                    'images': _multipart_images([
                        ('ok.png', _png_bytes('red')),
                        ('corrupt.png', b'definitely-not-an-image'),
                    ]),
                },
                content_type='multipart/form-data',
            )

            assert response.status_code == 400, response.get_json()
            observer = Session()
            # 整请求全有或全无：第二张损坏发生在任何 acquire 之前
            # （prepare 先于租约获取），不产生围栏，也不留排队行。
            assert observer.query(ImageImportItem).count() == 0
            assert _fence_state_summary(observer) == {
                'total': 0, 'held': 0, 'completed': 0, 'failed': 0,
            }


def test_import_queue_success_binds_items_atomically(concurrent_app):
    with concurrent_app.app_context():
        with _fenced_app(concurrent_app) as (storage, Session):
            client = concurrent_app.test_client()
            response = client.post(
                '/api/image-imports',
                data={
                    'images': _multipart_images([
                        ('ok-a.png', _png_bytes('red')),
                        ('ok-b.png', _png_bytes('green')),
                    ]),
                },
                content_type='multipart/form-data',
            )

            assert response.status_code in (200, 201, 202), response.get_json()
            observer = Session()
            assert observer.query(ImageImportItem).count() == 2
            assert _fence_state_summary(observer) == {
                'total': 4, 'held': 0, 'completed': 4, 'failed': 0,
            }


def test_disabled_fence_config_keeps_legacy_http_behavior(concurrent_app):
    """未启用围栏能力时，HTTP 写路径与今日一致：不产生围栏行。"""
    with concurrent_app.app_context():
        concurrent_app.config['INGEST_BINDING_FENCE_ENABLED'] = False
        concurrent_app.config['IMAGE_ASSET_STORAGE'] = FakeOss()
        concurrent_app.config['IMAGE_INGEST_EMBEDDING'] = FakeEmbedding()
        client = concurrent_app.test_client()
        response = client.post(
            '/api/products',
            data={
                'product': json.dumps({'model_number': 'FENCE-OFF'}),
                'images': _multipart_images([('a.png', _png_bytes('red'))]),
            },
            content_type='multipart/form-data',
        )
        assert response.status_code == 201
        Session = sessionmaker(bind=db.engine)
        observer = Session()
        try:
            assert observer.query(ImageAsset).count() == 1
            assert observer.query(ObjectBindingFence).count() == 0
        finally:
            observer.close()
            db.session.remove()


def test_product_creation_duplicate_content_same_request(concurrent_app):
    """risk review F1 回归：同请求同内容双图不得逐图 acquire 自锁。"""
    with concurrent_app.app_context():
        concurrent_app.config['IMAGE_INGEST_EMBEDDING'] = FakeEmbedding()
        with _fenced_app(concurrent_app) as (storage, Session):
            client = concurrent_app.test_client()
            response = client.post(
                '/api/products',
                data={
                    'product': json.dumps({'model_number': 'FENCE-DUP'}),
                    'images': _multipart_images([
                        ('dup-a.png', _png_bytes('red')),
                        ('dup-b.png', _png_bytes('red')),
                    ]),
                },
                content_type='multipart/form-data',
            )

            assert response.status_code == 201, response.get_json()
            observer = Session()
            assert observer.query(Product).count() == 1
            assert observer.query(ImageAsset).count() == 2
            # 去重后 3 把围栏（2 original + 1 共享 preview），随外层 commit 全释放。
            assert _fence_state_summary(observer) == {
                'total': 3, 'held': 0, 'completed': 3, 'failed': 0,
            }


def test_import_queue_duplicate_content_same_request(concurrent_app):
    with concurrent_app.app_context():
        with _fenced_app(concurrent_app) as (storage, Session):
            client = concurrent_app.test_client()
            response = client.post(
                '/api/image-imports',
                data={
                    'images': _multipart_images([
                        ('dup-a.png', _png_bytes('red')),
                        ('dup-b.png', _png_bytes('red')),
                    ]),
                },
                content_type='multipart/form-data',
            )

            assert response.status_code in (200, 201, 202), response.get_json()
            observer = Session()
            assert observer.query(ImageImportItem).count() == 2
            assert _fence_state_summary(observer) == {
                'total': 3, 'held': 0, 'completed': 3, 'failed': 0,
            }


def test_product_update_mixes_committed_existing_and_new_image_with_fences(concurrent_app):
    """N2：existing 早退与新写入必须共享安全 request transaction。"""
    with concurrent_app.app_context():
        concurrent_app.config['IMAGE_INGEST_EMBEDDING'] = FakeEmbedding()
        with _fenced_app(concurrent_app) as (storage, Session):
            client = concurrent_app.test_client()
            created = client.post(
                '/api/products',
                data={
                    'product': json.dumps({'model_number': 'FENCE-MIXED'}),
                    'images': _multipart_images([
                        ('existing.png', _png_bytes('red')),
                    ]),
                },
                content_type='multipart/form-data',
            )
            assert created.status_code == 201, created.get_json()
            initial_puts = tuple(storage.put_calls)
            observer = Session()
            existing = observer.query(ImageAsset).one()
            existing_vector = list(existing.vector)
            existing_source = (
                existing.source_provider,
                existing.source_bucket,
                existing.source_relative_path,
                existing.source_revision,
            )
            observer.close()

            updated = client.put(
                '/api/products/FENCE-MIXED',
                data={
                    'product': json.dumps({'category': 'mixed'}),
                    'images': _multipart_images([
                        ('existing.png', _png_bytes('red')),
                        ('new.png', _png_bytes('green')),
                    ]),
                },
                content_type='multipart/form-data',
            )

            assert updated.status_code == 200, updated.get_json()
            final = Session()
            assets = final.query(ImageAsset).order_by(ImageAsset.source_relative_path).all()
            assert len(assets) == 2
            unchanged = next(row for row in assets if row.id == existing.id)
            assert list(unchanged.vector) == existing_vector
            assert (
                unchanged.source_provider,
                unchanged.source_bucket,
                unchanged.source_relative_path,
                unchanged.source_revision,
            ) == existing_source
            assert tuple(storage.put_calls[:len(initial_puts)]) == initial_puts
            assert len(storage.put_calls) == 4
            assert _fence_state_summary(final)['held'] == 0


def test_import_queue_mixes_committed_existing_and_new_item_with_fences(concurrent_app):
    with concurrent_app.app_context():
        with _fenced_app(concurrent_app) as (storage, Session):
            client = concurrent_app.test_client()
            first = client.post(
                '/api/image-imports',
                data={
                    'images': _multipart_images([
                        ('existing.png', _png_bytes('red')),
                    ]),
                },
                content_type='multipart/form-data',
            )
            assert first.status_code == 202, first.get_json()
            initial = Session()
            existing = initial.query(ImageImportItem).one()
            existing_identity = (
                existing.id,
                existing.source_provider,
                existing.source_bucket,
                existing.source_relative_path,
                existing.source_revision,
                existing.content_hash,
            )
            initial.close()

            mixed = client.post(
                '/api/image-imports',
                data={
                    'images': _multipart_images([
                        ('existing.png', _png_bytes('red')),
                        ('new.png', _png_bytes('green')),
                    ]),
                },
                content_type='multipart/form-data',
            )

            assert mixed.status_code in (200, 202), mixed.get_json()
            final = Session()
            rows = final.query(ImageImportItem).order_by(ImageImportItem.created_at).all()
            assert len(rows) == 2
            unchanged = final.get(ImageImportItem, existing_identity[0])
            assert (
                unchanged.id,
                unchanged.source_provider,
                unchanged.source_bucket,
                unchanged.source_relative_path,
                unchanged.source_revision,
                unchanged.content_hash,
            ) == existing_identity
            assert len(storage.put_calls) == 4
            assert _fence_state_summary(final)['held'] == 0
