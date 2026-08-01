"""查询图 HTTP 边界到图片资产结果的纵向集成测试。"""

import io
import os

import numpy as np
from PIL import Image

from models import ImageAsset, db
from services.embedding import EmbeddingServiceError
from services.vector_search import VectorSearchService


def _png_bytes(color='red', size=(48, 32)):
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, format='PNG')
    return output.getvalue()


def _unit_vector(seed):
    vector = np.zeros(1024, dtype=np.float32)
    vector[seed] = 1.0
    return vector


def _asset(path, vector, *, model_number=None, status='active'):
    digest = path.encode('utf-8').hex()[:64].ljust(64, '0')
    return ImageAsset(
        model_number=model_number,
        source_provider='qiniu-kodo',
        source_bucket='xiangxipackage',
        source_relative_path=path,
        source_revision=1,
        oss_path=f'image-search/xiangxipackage/{path}',
        preview_oss_path=(
            f'image-search/previews/preview-v1/{digest[:2]}/{digest}.jpg'
        ),
        content_hash=digest,
        source_size=123,
        source_mime_type='image/png',
        source_width=48,
        source_height=32,
        vector=vector.tolist(),
        embedding_model='tongyi-embedding-vision-plus-2026-03-06',
        embedding_dimension=1024,
        normalization_version='preview-v1',
        status=status,
    )


class FakeDashScope:
    def __init__(self, vector=None, fail=False):
        self.vector = vector if vector is not None else _unit_vector(0)
        self.fail = fail
        self.paths = []
        self.payloads = []

    def embed_normalized_image(self, image_path, request_id=None):
        self.paths.append(image_path)
        with Image.open(image_path) as image:
            image.load()
            assert image.format == 'JPEG'
            assert max(image.size) <= 2048
        with open(image_path, 'rb') as source:
            self.payloads.append(source.read())
        if self.fail:
            raise EmbeddingServiceError('fake embedding unavailable')
        return self.vector


class FakeSigner:
    def __init__(self):
        self.signed = []
        self.write_calls = []

    def sign_download_url(self, key, expires_seconds):
        self.signed.append((key, expires_seconds))
        return f'https://private.example/{key}?signature=fake'

    def head_object(self, key):
        self.write_calls.append(('head', key))
        raise AssertionError('查询图不应进入 OSS')

    def put_file(self, key, source_path, *, spec):
        self.write_calls.append(('put_file', key))
        raise AssertionError('查询图不应进入 OSS')

    def put_bytes(self, key, data, *, spec):
        self.write_calls.append(('put_bytes', key))
        raise AssertionError('查询图不应进入 OSS')


def test_multipart_query_returns_ranked_unassigned_assets_and_private_preview(app):
    query_vector = _unit_vector(0)
    db.session.add_all([
        _asset('中文 目录/最相似.png', query_vector),
        _asset('同型号/另一张.png', _unit_vector(1), model_number=None),
        _asset('归档/不应返回.png', query_vector, status='archived'),
    ])
    db.session.commit()
    before_count = ImageAsset.query.count()

    embedding = FakeDashScope(query_vector)
    signer = FakeSigner()
    app.config['PRODUCT_SEARCH_SERVICE'] = VectorSearchService(
        embedding_client=embedding,
    )
    app.config['IMAGE_ASSET_STORAGE'] = signer

    client = app.test_client()
    response = client.post(
        '/api/products/search',
        data={
            'image': (io.BytesIO(_png_bytes()), '查询 图.png'),
            'top_k': '2',
        },
        content_type='multipart/form-data',
    )

    assert response.status_code == 200
    body = response.get_json()
    assert [item['relative_path'] for item in body] == [
        '中文 目录/最相似.png',
        '同型号/另一张.png',
    ]
    assert all(item['model_number'] is None for item in body)
    assert all(0.0 <= item['similarity'] <= 1.0 for item in body)
    assert ImageAsset.query.count() == before_count
    assert signer.write_calls == []
    assert embedding.paths and all(
        not os.path.exists(path) for path in embedding.paths
    )
    assert os.listdir(app.config['UPLOAD_FOLDER']) == []

    preview = client.get(body[0]['preview_url'])
    assert preview.status_code == 302
    assert preview.headers['Location'].startswith('https://private.example/')


def test_corrupt_query_returns_chinese_400_and_cleans_temporary_file(app):
    app.config['PRODUCT_SEARCH_SERVICE'] = VectorSearchService(
        embedding_client=FakeDashScope(),
    )

    response = app.test_client().post(
        '/api/products/search',
        data={'image': (io.BytesIO(b'not-an-image'), '损坏.jpg')},
        content_type='multipart/form-data',
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body['error_code'] == 'INVALID_IMAGE'
    assert '图片' in body['error']
    assert os.listdir(app.config['UPLOAD_FOLDER']) == []


def test_embedding_failure_returns_503_and_cleans_normalized_query(app):
    embedding = FakeDashScope(fail=True)
    app.config['PRODUCT_SEARCH_SERVICE'] = VectorSearchService(
        embedding_client=embedding,
    )

    response = app.test_client().post(
        '/api/products/search',
        data={'image': (io.BytesIO(_png_bytes()), '查询.png')},
        content_type='multipart/form-data',
    )

    assert response.status_code == 503
    assert response.get_json()['error_code'] == 'EMBEDDING_SERVICE_ERROR'
    assert embedding.paths and all(
        not os.path.exists(path) for path in embedding.paths
    )
    assert os.listdir(app.config['UPLOAD_FOLDER']) == []


def test_oversized_request_returns_chinese_413_without_persisting_query(app):
    app.config['MAX_CONTENT_LENGTH'] = 128
    app.config['PRODUCT_SEARCH_SERVICE'] = VectorSearchService(
        embedding_client=FakeDashScope(),
    )

    response = app.test_client().post(
        '/api/products/search',
        data={'image': (io.BytesIO(_png_bytes()), '太大.png')},
        content_type='multipart/form-data',
    )

    assert response.status_code == 413
    body = response.get_json()
    assert body['error_code'] == 'IMAGE_TOO_LARGE'
    assert '过大' in body['error']
    assert os.listdir(app.config['UPLOAD_FOLDER']) == []
