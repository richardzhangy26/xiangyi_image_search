"""Caller-owned ingest (commit=False + control session factory) never commits for the caller.

真实 PostgreSQL + 伪 OSS：注入 ``control_session_factory`` 后 ``commit=False`` 必须走
caller-owned 时序（acquire_prewrite → OSS 写入 → renew_prewrite →
finalize_in_transaction(caller session)），围栏服务自带的 session 全程不得被使用。

观察连接必须及时 close、调用方 session 必须 remove，否则夹具 teardown 的
DROP SCHEMA 会被 idle-in-transaction 连接锁住。
"""

from __future__ import annotations

import hashlib
import io
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

import numpy as np
from PIL import Image
from sqlalchemy.orm import sessionmaker

from models import ImageAsset, ObjectBindingFence, Product, db
from services.asset_ingest import ImageAssetIngestService
from services.embedding import EMBEDDING_DIMENSION, EmbeddingServiceError
from services.object_binding_fence import ObjectBindingFenceService
from services.object_source import SourceLocation, SourceObjectHead
from services.object_storage import StoredObject


def _png_bytes(color='red', size=(40, 24)):
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, format='PNG')
    return output.getvalue()


class FakeKodo:
    def __init__(self, objects, source_bucket='xiangxipackage'):
        self.objects = dict(objects)
        self.source_bucket = source_bucket

    def resolve_location(self):
        return SourceLocation(
            source_bucket=self.source_bucket,
            s3_bucket=self.source_bucket,
            s3_region='cn-east-1',
            endpoint_url='https://s3.cn-east-1.qiniucs.com',
        )

    def head_object(self, key):
        data = self.objects[key]
        return SourceObjectHead(
            key=key,
            size=len(data),
            content_type='image/png',
            etag='"fake"',
        )

    def download_object(self, key, target, *, max_bytes=None):
        data = self.objects[key]
        target.write(data)
        return len(data)


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
    def __init__(self, fail=False):
        self.fail = fail
        self.paths = []

    def embed_normalized_image(self, image_path, request_id=None):
        self.paths.append(image_path)
        if self.fail:
            raise EmbeddingServiceError('fake embedding failed')
        return np.full(EMBEDDING_DIMENSION, 0.1, dtype=np.float32)


class _NeverUsedSession:
    """caller-owned 路径必须完全经由 control factory，围栏自带 session 不许被触碰。"""

    def __getattr__(self, name):
        raise AssertionError(
            f'caller-owned ingest 不得使用围栏服务自带 session（{name}）'
        )

    def __call__(self):
        raise AssertionError('caller-owned ingest 不得使用围栏服务自带 session')


@contextmanager
def _scoped_sessions(app):
    """无论断言成败，退出时归还 observer 与调用方连接，避免 teardown 锁死。"""
    with app.app_context():
        observers = []

        def Session():
            session = sessionmaker(bind=db.engine)()
            observers.append(session)
            return session

        try:
            yield Session
        finally:
            for session in observers:
                session.close()
            db.session.remove()


def _caller_owned_service(Session, storage, embedding, relative_path):
    service = ImageAssetIngestService(
        source=FakeKodo({relative_path: _png_bytes()}),
        storage=storage,
        embedding_client=embedding,
        source_provider='product-upload',
        formal_bucket='formal-test-bucket',
        binding_fence_service=ObjectBindingFenceService(_NeverUsedSession()),
        control_session_factory=Session,
    )
    return service


def _caller_product(model_number):
    db.session.add(Product(
        model_number=model_number,
        photographer_file='p',
        alibaba_product_url='https://example.com/x',
        category='相机肩带',
    ))
    db.session.flush()


def _held_count(observer, lease):
    return observer.query(ObjectBindingFence).filter_by(
        owner_token=lease.owner_token, state='held',
    ).count()


def test_caller_owned_commit_releases_fence_only_when_outer_transaction_commits(concurrent_app):
    with _scoped_sessions(concurrent_app) as Session:
        relative_path = f'co-commit-{uuid.uuid4().hex}.png'
        storage = FakeOss()
        embedding = FakeEmbedding()
        service = _caller_owned_service(Session, storage, embedding, relative_path)
        _caller_product('CO-001')

        result = service.ingest_one(relative_path, model_number='CO-001', commit=False)

        assert result.status == 'created'
        lease = result.binding_lease
        assert lease is not None
        assert len(lease.fence_ids) == 2
        assert len(embedding.paths) == 1
        assert sorted(key.rsplit('/', 1)[-1] for key in storage.put_calls) == sorted(
            [relative_path, f'{result.content_hash}.jpg'],
        )
        # 服务没有替调用方提交：Product 行与资产行都还不能被其它会话看到，
        # 围栏仍是 control session 提交的 held 状态，释放随外层事务提交才生效。
        observer = Session()
        assert observer.query(ImageAsset).count() == 0
        assert observer.query(Product).count() == 0
        assert _held_count(observer, lease) == 2
        assert db.session().in_transaction() is True

        db.session.commit()
        observer.expire_all()
        assert observer.query(ImageAsset).count() == 1
        assert observer.query(ObjectBindingFence).filter_by(
            owner_token=lease.owner_token, state='released',
        ).count() == 2
        assert _held_count(observer, lease) == 0


def test_caller_owned_outer_rollback_keeps_held_fence_until_control_abort(concurrent_app):
    with _scoped_sessions(concurrent_app) as Session:
        relative_path = f'co-rollback-{uuid.uuid4().hex}.png'
        service = _caller_owned_service(
            Session, FakeOss(), FakeEmbedding(), relative_path,
        )
        _caller_product('CO-002')

        result = service.ingest_one(relative_path, model_number='CO-002', commit=False)
        lease = result.binding_lease
        assert lease is not None

        db.session.rollback()

        # 外层回滚后：资产未提交；围栏不丢所有权，明确保留至租约到期。
        observer = Session()
        assert observer.query(ImageAsset).count() == 0
        assert observer.query(Product).count() == 0
        assert _held_count(observer, lease) == 2

        # abort 经独立 control session 驱动：释放生效且可复核。
        assert service.abort_after_outer_rollback(lease) is True
        observer.expire_all()
        assert observer.query(ObjectBindingFence).filter_by(
            owner_token=lease.owner_token, state='released', release_reason='failed',
        ).count() == 2
        assert _held_count(observer, lease) == 0
        # 重复 abort 不再拥有租约，返回 False 而不是报错。
        assert service.abort_after_outer_rollback(lease) is False
        assert service.abort_after_outer_rollback(None) is False


def test_caller_owned_embedding_failure_releases_fence_without_touching_caller_transaction(concurrent_app):
    with _scoped_sessions(concurrent_app) as Session:
        relative_path = f'co-embedfail-{uuid.uuid4().hex}.png'
        storage = FakeOss()
        service = _caller_owned_service(
            Session, storage, FakeEmbedding(fail=True), relative_path,
        )
        _caller_product('CO-003')

        try:
            service.ingest_one(relative_path, model_number='CO-003', commit=False)
            raise AssertionError('embedding 失败必须向调用方传播')
        except EmbeddingServiceError:
            pass

        # finalize 之前的失败：服务通过 control session 释放围栏，不留 held 残留；
        # 已写入的对象为重试保留；调用方事务既未被提交也未被回滚。
        observer = Session()
        assert observer.query(ImageAsset).count() == 0
        assert observer.query(Product).count() == 0
        assert observer.query(ObjectBindingFence).filter_by(state='held').count() == 0
        assert observer.query(ObjectBindingFence).filter_by(
            state='released', release_reason='failed',
        ).count() == 2
        assert len(storage.put_calls) == 2
        assert db.session().in_transaction() is True
        db.session.rollback()


def test_none_control_factory_ingest_result_has_no_binding_lease(concurrent_app):
    """None 路径保持现状：不产生 caller-owned lease。"""
    with _scoped_sessions(concurrent_app) as Session:
        relative_path = f'co-none-{uuid.uuid4().hex}.png'
        service = ImageAssetIngestService(
            source=FakeKodo({relative_path: _png_bytes()}),
            storage=FakeOss(),
            embedding_client=FakeEmbedding(),
            source_provider='product-upload',
        )
        result = service.ingest_one(relative_path)
        assert result.status == 'created'
        assert result.binding_lease is None
        observer = Session()
        assert observer.query(ImageAsset).count() == 1
