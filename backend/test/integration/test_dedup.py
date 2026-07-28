"""SHA-256 全库精确去重。"""
import hashlib
import io
import os

import numpy as np
import pytest
from PIL import Image

from models import Product, ProductImage, db
from services.ingest import (
    ImageIngestService,
    PendingImage,
    hash_bytes,
    hash_file,
    storage_paths,
)


def _png_bytes(color):
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), color).save(buffer, format='PNG')
    return buffer.getvalue()


class FakeEmbedding:
    """记录调用次数，用于验证去重确实省下了 API 调用。"""

    def __init__(self):
        self.image_calls = 0
        self.batch_calls = 0

    def embed_image(self, image_path, request_id=None):
        self.image_calls += 1
        return np.full(1024, 0.1, dtype=np.float32)

    def embed_images(self, image_paths, request_id=None):
        self.batch_calls += 1
        self.image_calls += len(image_paths)
        return [np.full(1024, 0.1, dtype=np.float32) for _ in image_paths]


def _add_product(model_number):
    db.session.add(Product(
        model_number=model_number, photographer_file='p',
        alibaba_product_url='https://example.com/x', category='相机肩带',
    ))
    db.session.commit()


def test_hash_bytes_matches_hashlib():
    data = b'hello'
    assert hash_bytes(data) == hashlib.sha256(data).hexdigest()


def test_hash_file_matches_hash_bytes(tmp_path):
    data = _png_bytes('red')
    path = tmp_path / 'a.png'
    path.write_bytes(data)
    assert hash_file(str(path)) == hash_bytes(data)


def test_storage_paths_uses_hash_prefix_not_uuid():
    web, fs = storage_paths('/srv/uploads', 'CS-001', 'a' * 64, '.jpg')
    assert web == '/uploads/product_images/CS-001/aaaaaaaaaaaaaaaa.jpg'
    assert fs == '/srv/uploads/product_images/CS-001/aaaaaaaaaaaaaaaa.jpg'


def test_ingest_one_creates_row_and_file(app):
    _add_product('CS-001')
    embedding = FakeEmbedding()
    service = ImageIngestService(embedding_client=embedding)
    data = _png_bytes('red')

    result = service.ingest_one(
        'CS-001', data, '1.png', app.config['UPLOAD_FOLDER'], image_order=0, is_primary=True,
    )
    db.session.commit()

    assert result.status == 'created'
    assert result.content_hash == hash_bytes(data)
    assert ProductImage.query.count() == 1
    row = ProductImage.query.one()
    assert row.content_hash == hash_bytes(data)
    assert row.is_primary is True
    _, fs_path = storage_paths(app.config['UPLOAD_FOLDER'], 'CS-001', row.content_hash, '.png')
    with open(fs_path, 'rb') as handle:
        assert handle.read() == data
    assert embedding.image_calls == 1


def test_second_upload_of_same_bytes_is_skipped_without_api_call(app):
    """这正是磁盘上那 4 个同哈希文件的场景。"""
    _add_product('CS-001')
    embedding = FakeEmbedding()
    service = ImageIngestService(embedding_client=embedding)
    data = _png_bytes('red')

    service.ingest_one('CS-001', data, '1.png', app.config['UPLOAD_FOLDER'])
    db.session.commit()
    result = service.ingest_one('CS-001', data, '副本.png', app.config['UPLOAD_FOLDER'])
    db.session.commit()

    assert result.status == 'duplicate'
    assert result.duplicate_of == '/uploads/product_images/CS-001/' + hash_bytes(data)[:16] + '.png'
    assert ProductImage.query.count() == 1
    assert embedding.image_calls == 1  # 第二次没调 API


def test_dedup_is_global_across_products(app):
    """全库唯一：同一张图出现在另一个型号下也算重复。"""
    _add_product('CS-001')
    _add_product('HL-002')
    embedding = FakeEmbedding()
    service = ImageIngestService(embedding_client=embedding)
    data = _png_bytes('red')

    service.ingest_one('CS-001', data, '1.png', app.config['UPLOAD_FOLDER'])
    db.session.commit()
    result = service.ingest_one('HL-002', data, '主图.png', app.config['UPLOAD_FOLDER'])
    db.session.commit()

    assert result.status == 'duplicate'
    assert 'CS-001' in result.duplicate_of
    assert ProductImage.query.count() == 1


def test_different_images_both_ingested(app):
    _add_product('CS-001')
    service = ImageIngestService(embedding_client=FakeEmbedding())

    service.ingest_one('CS-001', _png_bytes('red'), '1.png', app.config['UPLOAD_FOLDER'])
    service.ingest_one('CS-001', _png_bytes('blue'), '2.png', app.config['UPLOAD_FOLDER'])
    db.session.commit()

    assert ProductImage.query.count() == 2


def test_ingest_one_removes_file_when_embedding_fails(app):
    """向量生成失败不能留下孤儿文件。"""
    from services.embedding import EmbeddingServiceError

    _add_product('CS-001')

    class FailingEmbedding:
        def embed_image(self, image_path, request_id=None):
            raise EmbeddingServiceError('boom')

    service = ImageIngestService(embedding_client=FailingEmbedding())
    data = _png_bytes('red')

    with pytest.raises(EmbeddingServiceError):
        service.ingest_one('CS-001', data, '1.png', app.config['UPLOAD_FOLDER'])

    db.session.rollback()
    _, fs_path = storage_paths(app.config['UPLOAD_FOLDER'], 'CS-001', hash_bytes(data), '.png')
    assert not os.path.exists(fs_path)


def test_ingest_pending_deduplicates_within_the_same_batch(app, tmp_path):
    """同一次运行里出现两份相同内容，只入库一次。"""
    _add_product('CS-001')
    data = _png_bytes('red')
    first = tmp_path / 'a.png'
    second = tmp_path / 'b.png'
    first.write_bytes(data)
    second.write_bytes(data)

    embedding = FakeEmbedding()
    service = ImageIngestService(embedding_client=embedding)
    digest = hash_bytes(data)
    pending = [
        PendingImage('CS-001', str(first), digest, 0, True),
        PendingImage('CS-001', str(second), digest, 1, False),
    ]

    results = service.ingest_pending(pending, app.config['UPLOAD_FOLDER'])
    db.session.commit()

    assert [r.status for r in results] == ['created', 'duplicate']
    assert ProductImage.query.count() == 1
    assert embedding.image_calls == 1


def test_ingest_pending_marks_failed_when_vector_is_none(app, tmp_path):
    _add_product('CS-001')
    path = tmp_path / 'a.png'
    path.write_bytes(_png_bytes('red'))

    class NoneEmbedding:
        def embed_images(self, image_paths, request_id=None):
            return [None] * len(image_paths)

    service = ImageIngestService(embedding_client=NoneEmbedding())
    pending = [PendingImage('CS-001', str(path), 'c' * 64, 0, True)]

    results = service.ingest_pending(pending, app.config['UPLOAD_FOLDER'])
    db.session.commit()

    assert results[0].status == 'failed'
    assert ProductImage.query.count() == 0


def test_ingest_pending_marks_batch_duplicate_failed_when_first_occurrence_fails(app, tmp_path):
    """批内查重是乐观的：重复项先被标 duplicate、duplicate_of 指向首现项*预期*的路径。

    如果首现项 embedding 失败，它从未落盘、DB 里也没有对应行 —— 这时依赖它的
    批内重复项绝不能停留在 duplicate（那意味着一张图被静默丢弃且没有报错信号）。
    必须回填为 failed。
    """
    _add_product('CS-001')
    data = _png_bytes('red')
    first = tmp_path / 'a.png'
    second = tmp_path / 'b.png'
    first.write_bytes(data)
    second.write_bytes(data)

    class FirstFailsEmbedding:
        def embed_images(self, image_paths, request_id=None):
            return [None] + [np.full(1024, 0.1, dtype=np.float32) for _ in image_paths[1:]]

    service = ImageIngestService(embedding_client=FirstFailsEmbedding())
    digest = hash_bytes(data)
    pending = [
        PendingImage('CS-001', str(first), digest, 0, True),
        PendingImage('CS-001', str(second), digest, 1, False),
    ]

    results = service.ingest_pending(pending, app.config['UPLOAD_FOLDER'])
    db.session.commit()

    assert [r.status for r in results] == ['failed', 'failed']
    assert results[1].duplicate_of is None
    assert results[1].error
    assert ProductImage.query.count() == 0


def test_ingest_one_result_exposes_fs_path_for_created_row(app):
    """调用方 rollback 时需要知道该删哪个文件——created 结果必须暴露文件系统绝对路径。"""
    _add_product('CS-001')
    service = ImageIngestService(embedding_client=FakeEmbedding())
    data = _png_bytes('red')

    result = service.ingest_one('CS-001', data, '1.png', app.config['UPLOAD_FOLDER'])
    db.session.commit()

    assert result.status == 'created'
    assert result.fs_path is not None
    assert os.path.isabs(result.fs_path)
    assert os.path.exists(result.fs_path)


def test_find_existing_hashes_chunks_large_input(app):
    """> 1000 个哈希要分块查询，不能一次塞进 IN 子句。"""
    from services.ingest import find_existing_hashes

    _add_product('CS-001')
    db.session.add(ProductImage(
        model_number='CS-001', image_path='/uploads/a.png',
        vector=[0.1] * 1024, content_hash='d' * 64,
    ))
    db.session.commit()

    probe = [f'{i:064d}' for i in range(2500)] + ['d' * 64]
    found = find_existing_hashes(probe)

    assert found == {'d' * 64: '/uploads/a.png'}
