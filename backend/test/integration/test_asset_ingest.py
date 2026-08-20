"""单张 Kodo 图片到私有 OSS 与 PostgreSQL 的纵向闭环。"""

from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pytest
from PIL import Image

from models import ImageAsset, Product, db
from services.asset_ingest import (
    AssetIngestConflictError,
    AssetIngestError,
    ImageAssetIngestService,
)
from services.embedding import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EmbeddingServiceError,
)
from services.image_normalizer import ImageNormalizer
from services.object_source import SourceLocation, SourceObjectHead
from services.object_storage import (
    ObjectStorageError,
    StoredObject,
)


def _png_bytes(color='red', size=(40, 24)):
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, format='PNG')
    return output.getvalue()


class FakeKodo:
    def __init__(self, objects, source_bucket='xiangxipackage'):
        self.objects = dict(objects)
        self.source_bucket = source_bucket
        self.temp_paths = []

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
        self.temp_paths.append(target.name)
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
            etag=hashlib.md5(
                item.data,
                usedforsecurity=False,
            ).hexdigest(),
        )

    def put_file(self, key, source_path, *, spec):
        assert key not in self.objects
        with open(source_path, 'rb') as source:
            data = source.read()
        assert hashlib.md5(data, usedforsecurity=False).hexdigest() == spec.md5_hex
        self.objects[key] = _FakeObject(
            data,
            spec.content_type,
            dict(spec.metadata),
        )
        self.put_calls.append(key)

    def put_bytes(self, key, data, *, spec):
        assert key not in self.objects
        assert hashlib.md5(data, usedforsecurity=False).hexdigest() == spec.md5_hex
        self.objects[key] = _FakeObject(
            data,
            spec.content_type,
            dict(spec.metadata),
        )
        self.put_calls.append(key)

    def sign_download_url(self, key, expires_seconds, *, cache_control=None):
        raise AssertionError('入库流程不应生成签名 URL')


class FakeEmbedding:
    def __init__(self, fail=False):
        self.fail = fail
        self.paths = []
        self.payloads = []
        self.batch_calls = []

    def embed_normalized_image(self, image_path, request_id=None):
        self.paths.append(image_path)
        with open(image_path, 'rb') as preview:
            self.payloads.append(preview.read())
        if self.fail:
            raise EmbeddingServiceError('fake embedding failed')
        return np.full(EMBEDDING_DIMENSION, 0.1, dtype=np.float32)

    def embed_normalized_images(self, image_paths, request_id=None):
        self.batch_calls.append(len(image_paths))
        return [
            self.embed_normalized_image(path, request_id=request_id)
            for path in image_paths
        ]


def _service(
    source,
    storage,
    embedding,
    *,
    normalizer=None,
    source_provider='qiniu-kodo',
):
    return ImageAssetIngestService(
        source=source,
        storage=storage,
        embedding_client=embedding,
        normalizer=normalizer,
        source_provider=source_provider,
    )


def test_one_unassigned_image_reaches_private_oss_and_postgresql(app):
    relative_path = '2025.4.18 海报照片/子目录/主图 一.png'
    original = _png_bytes()
    digest = hashlib.sha256(original).hexdigest()
    source = FakeKodo({relative_path: original})
    storage = FakeOss()
    embedding = FakeEmbedding()

    result = _service(source, storage, embedding).ingest_one(relative_path)

    assert result.status == 'created'
    row = ImageAsset.query.one()
    assert str(row.id) == result.asset_id
    assert row.model_number is None
    assert row.status == 'active'
    assert row.to_dict()['original_path'] is None
    assert row.source_relative_path == relative_path
    assert row.content_hash == digest
    assert row.embedding_model == EMBEDDING_MODEL
    assert row.embedding_dimension == EMBEDDING_DIMENSION

    original_key = f'image-search/xiangxipackage/{relative_path}'
    preview_key = (
        f'image-search/previews/preview-v1/{digest[:2]}/{digest}.jpg'
    )
    assert row.oss_path == original_key
    assert row.preview_oss_path == preview_key
    assert storage.head_calls == [original_key, preview_key]
    assert storage.objects[original_key].data == original
    assert hashlib.sha256(storage.objects[original_key].data).hexdigest() == digest
    assert len(storage.objects[preview_key].data) <= int(2.5 * 1024 * 1024)
    with Image.open(io.BytesIO(storage.objects[preview_key].data)) as preview:
        assert preview.format == 'JPEG'
    assert embedding.payloads == [storage.objects[preview_key].data]
    assert all(not os.path.exists(path) for path in source.temp_paths + embedding.paths)


def test_existing_oss_metadata_conflict_never_overwrites(app):
    relative_path = '冲突/图片.png'
    original = _png_bytes()
    source = FakeKodo({relative_path: original})
    storage = FakeOss()
    original_key = f'image-search/xiangxipackage/{relative_path}'
    storage.objects[original_key] = _FakeObject(
        data=b'unrelated',
        content_type='image/png',
        metadata={'sha256': '0' * 64},
    )
    embedding = FakeEmbedding()

    with pytest.raises(AssetIngestConflictError, match='冲突'):
        _service(source, storage, embedding).ingest_one(relative_path)

    assert storage.objects[original_key].data == b'unrelated'
    assert storage.put_calls == []
    assert embedding.paths == []
    assert ImageAsset.query.count() == 0
    assert all(not os.path.exists(path) for path in source.temp_paths)


def test_existing_oss_same_size_and_metadata_but_changed_content_conflicts(app):
    relative_path = '同尺寸篡改/图片.png'
    original = _png_bytes()
    digest = hashlib.sha256(original).hexdigest()
    source = FakeKodo({relative_path: original})
    storage = FakeOss()
    original_key = f'image-search/xiangxipackage/{relative_path}'
    tampered = bytes([original[0] ^ 1]) + original[1:]
    storage.objects[original_key] = _FakeObject(
        data=tampered,
        content_type='image/png',
        metadata={
            'source-provider': 'qiniu-kodo',
            'source-bucket': 'xiangxipackage',
            'sha256': digest,
            'source-size': str(len(original)),
        },
    )

    with pytest.raises(AssetIngestConflictError, match='原图对象冲突'):
        _service(source, storage, FakeEmbedding()).ingest_one(relative_path)

    assert storage.objects[original_key].data == tampered
    assert storage.put_calls == []
    assert ImageAsset.query.count() == 0


def test_existing_preview_content_conflict_never_overwrites(app):
    original = _png_bytes('purple')
    first_path = '预览冲突/第一张.png'
    second_path = '预览冲突/第二张.png'
    source = FakeKodo({
        first_path: original,
        second_path: original,
    })
    storage = FakeOss()
    embedding = FakeEmbedding()
    service = _service(source, storage, embedding)
    service.ingest_one(first_path)
    row = ImageAsset.query.one()
    preview_key = row.preview_oss_path
    preview_object = storage.objects[preview_key]
    tampered = bytes([preview_object.data[0] ^ 1]) + preview_object.data[1:]
    preview_object.data = tampered
    db.session.delete(row)
    db.session.commit()

    with pytest.raises(AssetIngestConflictError, match='搜索预览图对象冲突'):
        service.ingest_one(second_path)

    assert storage.objects[preview_key].data == tampered
    assert storage.put_calls.count(preview_key) == 1
    assert ImageAsset.query.count() == 0


def test_embedding_exception_cleans_all_temporary_files(app):
    relative_path = '异常/图片.png'
    source = FakeKodo({relative_path: _png_bytes()})
    storage = FakeOss()
    embedding = FakeEmbedding(fail=True)

    with pytest.raises(EmbeddingServiceError):
        _service(source, storage, embedding).ingest_one(relative_path)

    assert ImageAsset.query.count() == 0
    assert all(not os.path.exists(path) for path in source.temp_paths + embedding.paths)


def test_same_content_at_different_paths_creates_two_assets_and_reuses_vector(app):
    original = _png_bytes('blue')
    first_path = '目录一/同图.png'
    second_path = '目录二/同图副本.png'
    source = FakeKodo({
        first_path: original,
        second_path: original,
    })
    storage = FakeOss()
    embedding = FakeEmbedding()
    service = _service(source, storage, embedding)

    service.ingest_one(first_path)
    service.ingest_one(second_path)

    rows = ImageAsset.query.order_by(ImageAsset.source_relative_path).all()
    assert len(rows) == 2
    assert rows[0].content_hash == rows[1].content_hash
    assert rows[0].preview_oss_path == rows[1].preview_oss_path
    assert list(rows[0].vector) == list(rows[1].vector)
    assert len(embedding.paths) == 1
    assert storage.put_calls.count(rows[0].preview_oss_path) == 1


def test_same_source_rerun_revalidates_oss_and_is_idempotent(app):
    relative_path = '重跑/同一张.png'
    source = FakeKodo({relative_path: _png_bytes('cyan')})
    storage = FakeOss()
    embedding = FakeEmbedding()
    service = _service(source, storage, embedding)
    created = service.ingest_one(relative_path)
    storage.head_calls.clear()

    existing = service.ingest_one(relative_path)

    row = ImageAsset.query.one()
    assert existing.status == 'existing'
    assert existing.asset_id == created.asset_id
    assert storage.head_calls == [row.oss_path, row.preview_oss_path]
    assert len(embedding.paths) == 1
    assert ImageAsset.query.count() == 1


def test_archived_same_source_rerun_returns_recycle_bin_without_restoring(app):
    relative_path = '重跑/回收站.png'
    source = FakeKodo({relative_path: _png_bytes('cyan')})
    storage = FakeOss()
    embedding = FakeEmbedding()
    service = _service(source, storage, embedding)
    created = service.ingest_one(relative_path)
    asset = ImageAsset.query.one()
    asset.status = 'archived'
    asset.archived_at = datetime.now()
    archived_at = asset.archived_at
    db.session.commit()
    upload_count = len(storage.put_calls)

    result = service.ingest_one(relative_path)

    assert result.status == 'in_recycle_bin'
    assert result.asset_id == created.asset_id
    assert result.recovery_action == {
        'type': 'open_recycle_bin',
        'asset_id': created.asset_id,
    }
    db.session.expire_all()
    unchanged = ImageAsset.query.one()
    assert unchanged.status == 'archived'
    assert unchanged.archived_at == archived_at
    assert len(storage.put_calls) == upload_count
    assert len(embedding.paths) == 1


def test_same_source_path_with_changed_content_is_a_source_conflict(app):
    relative_path = '变化/图片.png'
    source = FakeKodo({relative_path: _png_bytes('red')})
    storage = FakeOss()
    embedding = FakeEmbedding()
    service = _service(source, storage, embedding)
    service.ingest_one(relative_path)
    source.objects[relative_path] = _png_bytes('green')

    with pytest.raises(AssetIngestConflictError, match='来源'):
        service.ingest_one(relative_path)

    assert ImageAsset.query.count() == 1
    assert len(embedding.paths) == 1


class CountingNormalizer:
    def __init__(self):
        self.delegate = ImageNormalizer()
        self.normalization_version = self.delegate.normalization_version
        self.calls = []

    def normalize(self, source_path):
        self.calls.append(str(source_path))
        return self.delegate.normalize(source_path)


def test_same_source_rerun_does_not_regenerate_preview(app):
    relative_path = '重跑/不重复标准化.png'
    source = FakeKodo({relative_path: _png_bytes('orange')})
    storage = FakeOss()
    embedding = FakeEmbedding()
    normalizer = CountingNormalizer()
    service = _service(
        source,
        storage,
        embedding,
        normalizer=normalizer,
    )

    service.ingest_one(relative_path)
    service.ingest_one(relative_path)

    assert len(normalizer.calls) == 1
    assert len(embedding.paths) == 1


def test_ingest_many_batches_unique_previews_at_requested_size(app):
    objects = {
        f'批量/{index:02d}.png': _png_bytes(
            (index, (index * 7) % 255, (index * 13) % 255)
        )
        for index in range(21)
    }
    source = FakeKodo(objects)
    storage = FakeOss()
    embedding = FakeEmbedding()

    results = _service(source, storage, embedding).ingest_many(
        list(objects),
        batch_size=20,
    )

    assert [result.status for result in results] == ['created'] * 21
    assert embedding.batch_calls == [20, 1]
    assert ImageAsset.query.count() == 21
    assert all(result.stages['database'] == 'new' for result in results)


def test_ingest_many_reuses_same_content_inside_one_batch(app):
    original = _png_bytes('blue')
    source = FakeKodo({
        '重复/第一张.png': original,
        '重复/第二张.png': original,
    })
    storage = FakeOss()
    embedding = FakeEmbedding()

    results = _service(source, storage, embedding).ingest_many([
        '重复/第一张.png',
        '重复/第二张.png',
    ])

    rows = ImageAsset.query.order_by(ImageAsset.source_relative_path).all()
    assert [result.status for result in results] == ['created', 'created']
    assert len(rows) == 2
    assert rows[0].preview_oss_path == rows[1].preview_oss_path
    assert list(rows[0].vector) == list(rows[1].vector)
    assert embedding.batch_calls == [1]
    assert len(embedding.paths) == 1
    assert results[1].stages['preview'] == 'reused'
    assert results[1].stages['embedding'] == 'reused'


def test_ingest_many_treats_repeated_source_path_as_idempotent(app):
    source = FakeKodo({'重复/同一路径.png': _png_bytes('blue')})
    embedding = FakeEmbedding()

    results = _service(source, FakeOss(), embedding).ingest_many([
        '重复/同一路径.png',
        '重复/同一路径.png',
    ])

    assert [result.status for result in results] == ['created', 'existing']
    assert results[0].asset_id == results[1].asset_id
    assert ImageAsset.query.count() == 1
    assert embedding.batch_calls == [1]


def test_ingest_many_isolates_bad_image_and_continues(app):
    source = FakeKodo({
        '批量/好图一.png': _png_bytes('red'),
        '批量/损坏.png': b'not-an-image',
        '批量/好图二.png': _png_bytes('green'),
    })

    results = _service(source, FakeOss(), FakeEmbedding()).ingest_many([
        '批量/好图一.png',
        '批量/损坏.png',
        '批量/好图二.png',
    ])

    assert [result.status for result in results] == [
        'created',
        'failed',
        'created',
    ]
    assert results[1].error_stage == 'preview'
    assert results[1].stages == {'download': 'new'}
    assert ImageAsset.query.count() == 2


def test_ingest_many_preserves_download_when_original_upload_fails(app):
    class FirstOriginalFailsStorage(FakeOss):
        def __init__(self):
            super().__init__()
            self.failed = False

        def put_file(self, key, source_path, *, spec):
            if not self.failed:
                self.failed = True
                raise ObjectStorageError('fake original failure')
            return super().put_file(key, source_path, spec=spec)

    source = FakeKodo({
        '批量/原图失败.png': _png_bytes('red'),
        '批量/后续成功.png': _png_bytes('green'),
    })

    results = _service(
        source,
        FirstOriginalFailsStorage(),
        FakeEmbedding(),
    ).ingest_many(list(source.objects))

    assert [result.status for result in results] == ['failed', 'created']
    assert results[0].error_stage == 'original'
    assert results[0].stages == {'download': 'new'}
    assert ImageAsset.query.one().source_relative_path == '批量/后续成功.png'


def test_ingest_many_preserves_original_when_preview_upload_fails(app):
    class FirstPreviewFailsStorage(FakeOss):
        def __init__(self):
            super().__init__()
            self.failed = False

        def put_bytes(self, key, data, *, spec):
            if not self.failed:
                self.failed = True
                raise ObjectStorageError('fake preview failure')
            return super().put_bytes(key, data, spec=spec)

    source = FakeKodo({
        '批量/预览失败.png': _png_bytes('red'),
        '批量/后续成功.png': _png_bytes('green'),
    })

    results = _service(
        source,
        FirstPreviewFailsStorage(),
        FakeEmbedding(),
    ).ingest_many(list(source.objects))

    assert [result.status for result in results] == ['failed', 'created']
    assert results[0].error_stage == 'preview'
    assert results[0].stages == {
        'download': 'new',
        'original': 'new',
    }
    assert ImageAsset.query.one().source_relative_path == '批量/后续成功.png'


def test_ingest_many_isolates_invalid_vector_and_persists_other_items(app):
    class InvalidFirstVectorEmbedding(FakeEmbedding):
        def embed_normalized_images(self, image_paths, request_id=None):
            self.batch_calls.append(len(image_paths))
            return [
                np.array([0.1], dtype=np.float32),
                np.full(EMBEDDING_DIMENSION, 0.2, dtype=np.float32),
            ]

    source = FakeKodo({
        '批量/错维向量.png': _png_bytes('red'),
        '批量/正常向量.png': _png_bytes('green'),
    })

    results = _service(
        source,
        FakeOss(),
        InvalidFirstVectorEmbedding(),
    ).ingest_many(list(source.objects))

    assert [result.status for result in results] == ['failed', 'created']
    assert results[0].error_stage == 'embedding'
    assert results[0].stages == {
        'download': 'new',
        'original': 'new',
        'preview': 'new',
    }
    assert ImageAsset.query.one().source_relative_path == '批量/正常向量.png'


def test_ingest_many_preserves_completed_stages_when_database_write_fails(
    app,
    monkeypatch,
):
    source = FakeKodo({
        '批量/数据库失败.png': _png_bytes('red'),
        '批量/后续成功.png': _png_bytes('green'),
    })
    service = _service(source, FakeOss(), FakeEmbedding())
    real_persist = service._persist
    calls = 0

    def fail_first_persist(prepared, vector_values, *, commit):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AssetIngestError('fake database failure', stage='database')
        return real_persist(prepared, vector_values, commit=commit)

    monkeypatch.setattr(service, '_persist', fail_first_persist)

    results = service.ingest_many(list(source.objects))

    assert [result.status for result in results] == ['failed', 'created']
    assert results[0].error_stage == 'database'
    assert results[0].stages == {
        'download': 'new',
        'original': 'new',
        'preview': 'new',
        'embedding': 'new',
    }
    assert ImageAsset.query.one().source_relative_path == '批量/后续成功.png'


def test_non_kodo_source_and_caller_owned_transaction(app):
    db.session.add(Product(
        model_number='UPLOAD-001',
        photographer_file='p',
        alibaba_product_url='https://example.com/x',
        category='相机肩带',
    ))
    source = FakeKodo(
        {'UPLOAD-001/原图.png': _png_bytes('purple')},
        source_bucket='product-crud',
    )
    service = _service(
        source,
        FakeOss(),
        FakeEmbedding(),
        source_provider='product-upload',
    )

    result = service.ingest_one(
        'UPLOAD-001/原图.png',
        model_number='UPLOAD-001',
        commit=False,
    )

    assert result.status == 'created'
    assert ImageAsset.query.one().source_provider == 'product-upload'
    db.session.rollback()
    assert ImageAsset.query.count() == 0
    assert Product.query.count() == 0


def test_ingest_many_refuses_to_commit_callers_pending_writes(app):
    db.session.add(Product(
        model_number='PENDING-001',
        photographer_file='p',
        alibaba_product_url='https://example.com/x',
        category='相机肩带',
    ))
    source = FakeKodo({'批量/图片.png': _png_bytes('red')})

    with pytest.raises(AssetIngestError, match='已有数据库写事务'):
        _service(source, FakeOss(), FakeEmbedding()).ingest_many(
            ['批量/图片.png']
        )

    assert Product.query.count() == 1
    assert ImageAsset.query.count() == 0
    db.session.rollback()
