"""写入与删除路径：不吞异常、清理磁盘文件、重复图片明确提示。"""
import io
import json
import os

import numpy as np
from PIL import Image

from models import Product, ProductImage, db
from services.embedding import EmbeddingServiceError


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
