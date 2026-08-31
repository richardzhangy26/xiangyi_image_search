"""chunk-owner 协议在 control-factory 模式下的 batch/queue 语义（真实 PostgreSQL + 伪 OSS）。

architect 协议：``_ingest_batch`` 整批一次 acquire 完整去重 identity 集；写对象、
保持单次 ``embed_normalized_images`` 批量调用；逐 item final-bind 时只释放该 item
的独占 original，共享 preview 在最后一个 consumer 绑定（或整批收尾清扫）后才释放；
无效向量 item 隔离且不留 held 残留。围栏服务自带 session 全程不得被使用
（毒化 session 证明），租约生命周期只经 ``control_session_factory``。
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

from models import ImageAsset, ImageImportItem, ObjectBindingFence, db
from services.asset_ingest import ImageAssetIngestService
from services.embedding import EMBEDDING_DIMENSION
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
            key=key, size=len(data), content_type='image/png', etag='"fake"',
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
    """按预览内容决定坏向量：payload 命中 bad_payloads 时返回错维向量。

    ``fail_on_nth_single``：第 N 次单图 embedding 调用直接抛 EmbeddingServiceError，
    供产品多图 HTTP 测试模拟中途失败。
    """

    def __init__(self, bad_payloads=(), fail_on_nth_single=None):
        self.batch_calls = []
        self.single_calls = 0
        self.bad_payloads = set(bad_payloads)
        self.fail_on_nth_single = fail_on_nth_single

    def embed_normalized_image(self, image_path, request_id=None):
        self.single_calls += 1
        from services.embedding import EmbeddingServiceError
        if (
            self.fail_on_nth_single is not None
            and self.single_calls == self.fail_on_nth_single
        ):
            raise EmbeddingServiceError('fake embedding failed')
        with open(image_path, 'rb') as preview:
            payload = preview.read()
        if payload in self.bad_payloads:
            return np.zeros(5, dtype=np.float32)
        return np.full(EMBEDDING_DIMENSION, 0.1, dtype=np.float32)

    def embed_normalized_images(self, image_paths, request_id=None):
        self.batch_calls.append(len(image_paths))
        return [
            self.embed_normalized_image(path, request_id=request_id)
            for path in image_paths
        ]


class _NeverUsedSession:
    def __getattr__(self, name):
        raise AssertionError(f'control-factory 模式不得使用围栏自带 session（{name}）')

    def __call__(self):
        raise AssertionError('control-factory 模式不得使用围栏自带 session')


@contextmanager
def _scoped_sessions(app):
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


def _control_service(Session, source, storage, embedding):
    return ImageAssetIngestService(
        source=source,
        storage=storage,
        embedding_client=embedding,
        source_provider='qiniu-kodo',
        formal_bucket='formal-test-bucket',
        binding_fence_service=ObjectBindingFenceService(_NeverUsedSession()),
        control_session_factory=Session,
    )


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


def test_ingest_many_control_lease_shared_preview_single_put_and_release_all(concurrent_app):
    with _scoped_sessions(concurrent_app) as Session:
        red = _png_bytes('red')
        green = _png_bytes('green')
        source = FakeKodo({
            'bc27/a.png': red,
            'bc27/b.png': red,   # 同内容 → 共享 preview
            'bc27/c.png': green,
        })
        storage = FakeOss()
        embedding = FakeEmbedding()
        service = _control_service(Session, source, storage, embedding)

        results = service.ingest_many(list(source.objects))

        assert [result.status for result in results] == ['created'] * 3
        # 同内容去重后 2 个代表 → 单次批量 embedding 调用。
        assert embedding.batch_calls == [2]
        red_hash = hashlib.sha256(red).hexdigest()
        preview_key = f'image-search/previews/preview-v1/{red_hash[:2]}/{red_hash}.jpg'
        assert sum(1 for key in storage.put_calls if key == preview_key) == 1
        observer = Session()
        assert observer.query(ImageAsset).count() == 3
        # 整批 5 个去重 identity（3 original + 2 preview）全部 released，无 held 残留；
        # 独占 original 随各自 item 绑定释放，共享 preview 在最后一个 consumer 之后释放。
        summary = _fence_state_summary(observer)
        assert summary == {
            'total': 5, 'held': 0, 'completed': 5, 'failed': 0,
        }


def test_ingest_many_control_lease_invalid_vector_isolated_and_swept(concurrent_app):
    with _scoped_sessions(concurrent_app) as Session:
        red = _png_bytes('red')
        green = _png_bytes('green')
        # 坏向量按预览内容识别：取 green 原图标准化后的 preview 文件不可预知，
        # 因此这里用一个包装 embedding：对第 2 个代表返回错维向量。
        class BadSecondEmbedding(FakeEmbedding):
            def embed_normalized_images(self, image_paths, request_id=None):
                self.batch_calls.append(len(image_paths))
                vectors = []
                for index, path in enumerate(image_paths):
                    if index == 1:
                        vectors.append(np.zeros(5, dtype=np.float32))
                    else:
                        vectors.append(
                            np.full(EMBEDDING_DIMENSION, 0.1, dtype=np.float32),
                        )
                return vectors

        source = FakeKodo({
            'bc27-bad/a.png': red,
            'bc27-bad/b.png': green,
        })
        storage = FakeOss()
        service = _control_service(
            Session, source, storage, BadSecondEmbedding(),
        )

        results = service.ingest_many(
            ['bc27-bad/a.png', 'bc27-bad/b.png'], batch_size=20,
        )

        by_path = {
            result.source_relative_path: result for result in results
        }
        assert by_path['bc27-bad/a.png'].status == 'created'
        assert by_path['bc27-bad/b.png'].status == 'failed'
        assert by_path['bc27-bad/b.png'].error_stage == 'embedding'
        observer = Session()
        assert observer.query(ImageAsset).count() == 1
        # 失败 item 的对象为重试保留（不删除），但其独占围栏经 control 收尾清扫释放；
        # 成功 item 的围栏随绑定释放。全批不留 held 残留。
        summary = _fence_state_summary(observer)
        assert summary == {
            'total': 4, 'held': 0, 'completed': 2, 'failed': 2,
        }
        assert len(storage.put_calls) == 4  # 2 original + 2 preview 全部保留


def test_queue_one_control_lease_binds_item_and_releases_without_fence_session(concurrent_app):
    with _scoped_sessions(concurrent_app) as Session:
        relative_path = f'bq27/{uuid.uuid4().hex}.png'
        source = FakeKodo({relative_path: _png_bytes('purple')})
        storage = FakeOss()
        embedding = FakeEmbedding()
        service = _control_service(Session, source, storage, embedding)

        result = service.queue_one(relative_path, request_id='issue-27-queue-control')

        assert result.status == 'queued'
        assert result.item_id is not None
        assert embedding.single_calls == 0  # 排队路径不在请求内 embedding
        observer = Session()
        item = observer.get(ImageImportItem, uuid.UUID(result.item_id))
        assert item is not None and item.status == 'queued'
        assert observer.query(ImageAsset).count() == 0
        summary = _fence_state_summary(observer)
        assert summary == {
            'total': 2, 'held': 0, 'completed': 2, 'failed': 0,
        }


def test_ingest_many_caller_owned_duplicate_content_one_lease_no_self_lock(concurrent_app):
    """risk review F1：同请求同内容多图必须走请求级一个去重租约，禁止逐图 acquire 自锁。"""
    with _scoped_sessions(concurrent_app) as Session:
        red = _png_bytes('red')
        source = FakeKodo({
            'f1/a.png': red,
            'f1/b.png': red,
        })
        storage = FakeOss()
        embedding = FakeEmbedding()
        service = _control_service(Session, source, storage, embedding)

        results, lease = service.ingest_many_caller_owned(
            ['f1/a.png', 'f1/b.png'],
            model_number=None,
            request_id='issue-27-f1',
        )

        assert [result.status for result in results] == ['created', 'created']
        assert lease is not None
        # 整请求一次 acquire：去重后 3 个 identity（2 original + 1 共享 preview）。
        assert len(set(lease.fence_ids)) == 3
        observer = Session()
        # 服务没有替调用方提交：commit 前观察不到资产。
        assert observer.query(ImageAsset).count() == 0
        assert observer.query(ObjectBindingFence).filter_by(state='held').count() == 3

        db.session.commit()
        observer.expire_all()
        assert observer.query(ImageAsset).count() == 2
        summary = _fence_state_summary(observer)
        assert summary == {'total': 3, 'held': 0, 'completed': 3, 'failed': 0}
        # 共享 preview 只 PUT 一次。
        red_hash = hashlib.sha256(red).hexdigest()
        preview_key = f'image-search/previews/preview-v1/{red_hash[:2]}/{red_hash}.jpg'
        assert sum(1 for key in storage.put_calls if key == preview_key) == 1


def test_queue_many_caller_owned_duplicate_content_one_lease(concurrent_app):
    with _scoped_sessions(concurrent_app) as Session:
        red = _png_bytes('red')
        source = FakeKodo({'f1q/a.png': red, 'f1q/b.png': red})
        service = _control_service(
            Session, source, FakeOss(), FakeEmbedding(),
        )

        results, lease = service.queue_many_caller_owned(
            ['f1q/a.png', 'f1q/b.png'],
            request_id='issue-27-f1q',
        )

        assert [result.status for result in results] == ['queued', 'queued']
        assert lease is not None and len(set(lease.fence_ids)) == 3
        observer = Session()
        assert observer.query(ImageImportItem).count() == 0
        assert observer.query(ObjectBindingFence).filter_by(state='held').count() == 3

        db.session.commit()
        observer.expire_all()
        assert observer.query(ImageImportItem).count() == 2
        summary = _fence_state_summary(observer)
        assert summary == {'total': 3, 'held': 0, 'completed': 3, 'failed': 0}


def test_finalize_after_failure_lease_attached_for_boundary_reclaim(concurrent_app):
    """risk review F2：finalize 已开始后失败，租约挂在异常上供边界回滚后回收。"""
    from services.asset_ingest import AssetIngestError

    class ExplodingService(ImageAssetIngestService):
        def _persist(self, prepared, vector_values, *, commit):
            self._boom_count = getattr(self, '_boom_count', 0) + 1
            if self._boom_count == 2:
                raise AssetIngestError('boom', stage='database')
            return super()._persist(prepared, vector_values, commit=commit)

    with _scoped_sessions(concurrent_app) as Session:
        source = FakeKodo({
            'f2/a.png': _png_bytes('red'),
            'f2/b.png': _png_bytes('green'),
        })
        service = ExplodingService(
            source=source,
            storage=FakeOss(),
            embedding_client=FakeEmbedding(),
            source_provider='qiniu-kodo',
            formal_bucket='formal-test-bucket',
            binding_fence_service=ObjectBindingFenceService(_NeverUsedSession()),
            control_session_factory=Session,
        )
        try:
            service.ingest_many_caller_owned(
                ['f2/a.png', 'f2/b.png'], request_id='issue-27-f2',
            )
            raise AssertionError('第二个 item 绑定失败必须抛出')
        except AssetIngestError as exc:
            lease = getattr(exc, 'binding_fence_lease', None)
            assert lease is not None
            db.session.rollback()
            assert service.abort_after_outer_rollback(lease) is True
        observer = Session()
        assert observer.query(ImageAsset).count() == 0
        summary = _fence_state_summary(observer)
        assert summary == {'total': 4, 'held': 0, 'completed': 0, 'failed': 4}
