"""写入与删除路径：不吞异常、清理磁盘文件、重复图片明确提示。"""
import io
import json
import os

import numpy as np
from PIL import Image

from models import Product, ProductImage, db
from services.embedding import EmbeddingServiceError
from services.ingest import hash_bytes, storage_paths


def _png_bytes(color):
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), color).save(buffer, format='PNG')
    return buffer.getvalue()


class FakeEmbedding:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def embed_image(self, image_path, request_id=None):
        self.calls += 1
        if self.fail:
            raise EmbeddingServiceError('boom')
        return np.full(1024, 0.1, dtype=np.float32)

    def embed_images(self, image_paths, request_id=None):
        return [self.embed_image(p, request_id) for p in image_paths]


def _install_embedding(app, embedding):
    from services.vector_search import VectorSearchService
    app.config['PRODUCT_SEARCH_SERVICE'] = VectorSearchService(embedding_client=embedding)
    app.config['IMAGE_INGEST_EMBEDDING'] = embedding


def _product_payload(model_number):
    return json.dumps({
        'model_number': model_number,
        'photographer_file': 'p',
        'alibaba_product_url': 'https://example.com/x',
        'category': '相机肩带',
    })


def test_create_product_reports_duplicate_images(app):
    _install_embedding(app, FakeEmbedding())
    client = app.test_client()
    data = _png_bytes('red')

    response = client.post('/api/products', data={
        'product': _product_payload('CS-001'),
        'images': [(io.BytesIO(data), '1.png'), (io.BytesIO(data), '副本.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 201
    body = response.get_json()
    assert body['uploaded_images'] == 1
    assert len(body['skipped_duplicates']) == 1
    assert ProductImage.query.count() == 1


def test_update_product_returns_503_when_embedding_fails(app):
    """旧行为：只 log，返回 200「更新成功」，图片文件留在磁盘上，数据静默丢失。"""
    _install_embedding(app, FakeEmbedding())
    client = app.test_client()
    client.post('/api/products', data={'product': _product_payload('CS-001')},
                content_type='multipart/form-data')

    _install_embedding(app, FakeEmbedding(fail=True))
    response = client.put('/api/products/CS-001', data={
        'images': [(io.BytesIO(_png_bytes('red')), '1.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 503
    assert response.get_json()['error_code'] == 'EMBEDDING_SERVICE_ERROR'
    assert ProductImage.query.count() == 0

    # 失败的图片文件不得留在磁盘上
    product_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'product_images', 'CS-001')
    assert not os.path.isdir(product_dir) or os.listdir(product_dir) == []


def test_delete_product_removes_files_from_disk(app):
    _install_embedding(app, FakeEmbedding())
    client = app.test_client()
    client.post('/api/products', data={
        'product': _product_payload('CS-001'),
        'images': [(io.BytesIO(_png_bytes('red')), '1.png')],
    }, content_type='multipart/form-data')

    fs_path = ProductImage.query.one().original_path
    assert os.path.exists(fs_path)

    response = client.delete('/api/products/CS-001')

    assert response.status_code == 200
    assert ProductImage.query.count() == 0
    assert not os.path.exists(fs_path)


def test_batch_delete_removes_files_from_disk(app):
    _install_embedding(app, FakeEmbedding())
    client = app.test_client()
    for model_number, color in (('CS-001', 'red'), ('CS-002', 'blue')):
        client.post('/api/products', data={
            'product': _product_payload(model_number),
            'images': [(io.BytesIO(_png_bytes(color)), '1.png')],
        }, content_type='multipart/form-data')

    paths = [row.original_path for row in ProductImage.query.all()]
    assert len(paths) == 2 and all(os.path.exists(p) for p in paths)

    response = client.post('/api/products/batch-delete',
                           json={'model_numbers': ['CS-001', 'CS-002']})

    assert response.status_code == 200
    assert response.get_json()['deleted_count'] == 2
    assert ProductImage.query.count() == 0
    assert not any(os.path.exists(p) for p in paths)


def test_delete_single_image_removes_file(app):
    _install_embedding(app, FakeEmbedding())
    client = app.test_client()
    client.post('/api/products', data={
        'product': _product_payload('CS-001'),
        'images': [(io.BytesIO(_png_bytes('red')), '1.png')],
    }, content_type='multipart/form-data')

    row = ProductImage.query.one()
    fs_path = row.original_path

    response = client.delete(f'/api/products/CS-001/images/{row.id}')

    assert response.status_code == 200
    assert not os.path.exists(fs_path)


def test_reupload_after_delete_is_allowed(app):
    """删除释放了哈希，同一张图可以重新上传。"""
    _install_embedding(app, FakeEmbedding())
    client = app.test_client()
    data = _png_bytes('red')
    client.post('/api/products', data={
        'product': _product_payload('CS-001'),
        'images': [(io.BytesIO(data), '1.png')],
    }, content_type='multipart/form-data')

    row_id = ProductImage.query.one().id
    client.delete(f'/api/products/CS-001/images/{row_id}')

    response = client.put('/api/products/CS-001', data={
        'images': [(io.BytesIO(data), '1.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    assert ProductImage.query.count() == 1


def test_create_product_concurrent_duplicate_returns_409(app, monkeypatch):
    """并发上传同一张*新*图的竞态窗口：两边判重都过，后 commit 的一方撞 UNIQUE 约束。

    用 monkeypatch 让 find_existing_hashes 对已存在的哈希也返回空 dict 来模拟这个窗口，
    避免真的起两个线程/进程去赌时序（竞态成因见 services/ingest.py 的 ingest_one docstring）。
    """
    _install_embedding(app, FakeEmbedding())
    client = app.test_client()
    data = _png_bytes('red')

    # 先建立一行已提交的记录，占住这张图的 content_hash
    client.post('/api/products', data={
        'product': _product_payload('CS-001'),
        'images': [(io.BytesIO(data), '1.png')],
    }, content_type='multipart/form-data')
    assert ProductImage.query.count() == 1

    # 判重"假阴性"：伪装这张图从未出现过，让 ingest_one 走到 created 分支
    monkeypatch.setattr('services.ingest.find_existing_hashes', lambda hashes: {})

    response = client.post('/api/products', data={
        'product': _product_payload('CS-002'),
        'images': [(io.BytesIO(data), '1.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 409
    assert response.get_json()['error_code'] == 'DUPLICATE_IMAGE_CONFLICT'

    db.session.expire_all()  # 强制下面的查询打到数据库，不读会话内的缓存对象
    assert Product.query.get('CS-002') is None
    assert ProductImage.query.count() == 1

    # rollback 时必须用 fs_path 删掉已落盘的文件，不能留孤儿
    _, fs_path = storage_paths(app.config['UPLOAD_FOLDER'], 'CS-002', hash_bytes(data), '.png')
    assert not os.path.exists(fs_path)


def test_update_product_concurrent_duplicate_returns_409_and_rolls_back(app, monkeypatch):
    """update 路径同样的竞态窗口：字段更新与新图片同属一个事务，必须一起回滚，不能半成功。"""
    _install_embedding(app, FakeEmbedding())
    client = app.test_client()
    data = _png_bytes('blue')

    # 另一个产品先占住这张图的 content_hash
    client.post('/api/products', data={
        'product': _product_payload('HL-002'),
        'images': [(io.BytesIO(data), '1.png')],
    }, content_type='multipart/form-data')

    # 待更新的产品，本身没有图片
    client.post('/api/products', data={'product': _product_payload('CS-001')},
                content_type='multipart/form-data')
    image_count_before = ProductImage.query.count()

    monkeypatch.setattr('services.ingest.find_existing_hashes', lambda hashes: {})

    response = client.put('/api/products/CS-001', data={
        'product': json.dumps({'photographer_file': 'changed'}),
        'images': [(io.BytesIO(data), '1.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 409
    assert response.get_json()['error_code'] == 'DUPLICATE_IMAGE_CONFLICT'

    db.session.expire_all()  # 强制下面的查询打到数据库，不读会话内的缓存对象
    # 字段更新和图片写入同属一个事务：字段必须保持修改前的值，不能只回滚一半
    assert Product.query.get('CS-001').photographer_file == 'p'
    assert ProductImage.query.count() == image_count_before

    _, fs_path = storage_paths(app.config['UPLOAD_FOLDER'], 'CS-001', hash_bytes(data), '.png')
    assert not os.path.exists(fs_path)


def test_update_product_reports_duplicate_image(app):
    """PUT 上传一张库里已存在的图：明确上报为跳过，而不是静默丢弃或误当新图入库。"""
    _install_embedding(app, FakeEmbedding())
    client = app.test_client()
    data = _png_bytes('red')

    client.post('/api/products', data={
        'product': _product_payload('CS-001'),
        'images': [(io.BytesIO(data), '1.png')],
    }, content_type='multipart/form-data')

    original_image_path = ProductImage.query.one().image_path

    response = client.put('/api/products/CS-001', data={
        'images': [(io.BytesIO(data), '2.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    body = response.get_json()
    assert body['uploaded_images'] == 0
    assert len(body['skipped_duplicates']) == 1
    assert body['skipped_duplicates'][0]['duplicate_of'] == original_image_path
    assert ProductImage.query.count() == 1
