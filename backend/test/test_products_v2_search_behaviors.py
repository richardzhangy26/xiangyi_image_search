import io
import json
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app import create_app
from models import db, Product, ProductImage
from product_search import EmbeddingServiceError, VectorSearchError


class FakeSearchService:
    def __init__(self, results=None, fail=None):
        self.results = results or []
        self.fail = fail

    def search_similar_images(self, image_path, top_k=10, request_id=None):
        if self.fail:
            raise self.fail
        return self.results


class FakeCreateService:
    def __init__(self, exc=None):
        self.exc = exc

    def extract_feature(self, image_path, request_id=None):
        if self.exc:
            raise self.exc
        return [0.1] * 1024


def _build_client_with_db():
    app = create_app('testing')
    app.config['UPLOAD_FOLDER'] = '/tmp/xiangyi_test_uploads'
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    with app.app_context():
        db.create_all()

    return app, app.test_client()


def _seed_products(app):
    with app.app_context():
        p1 = Product(
            model_number='M-001',
            photographer_file='p1',
            alibaba_product_url='https://example.com/1',
            category='cat',
        )
        p2 = Product(
            model_number='M-002',
            photographer_file='p2',
            alibaba_product_url='https://example.com/2',
            category='cat',
        )
        db.session.add_all([p1, p2])
        db.session.commit()


def test_search_top_k_invalid_returns_400():
    app, client = _build_client_with_db()
    _seed_products(app)
    app.config['PRODUCT_SEARCH_SERVICE'] = FakeSearchService(results=[])

    for invalid_top_k in ['0', '-1', '9999', 'abc']:
        response = client.post(
            '/api/products/search',
            data={'image': (io.BytesIO(b'img'), 'a.jpg'), 'top_k': invalid_top_k},
            content_type='multipart/form-data',
        )

        assert response.status_code == 400
        body = response.get_json()
        assert body['error_code'] == 'INVALID_TOP_K'


def test_search_embedding_failure_returns_503():
    app, client = _build_client_with_db()
    _seed_products(app)
    app.config['PRODUCT_SEARCH_SERVICE'] = FakeSearchService(
        fail=EmbeddingServiceError('embedding down')
    )

    response = client.post(
        '/api/products/search',
        data={'image': (io.BytesIO(b'img'), 'a.jpg')},
        content_type='multipart/form-data',
    )

    assert response.status_code == 503
    body = response.get_json()
    assert body['error_code'] == 'EMBEDDING_SERVICE_ERROR'


def test_search_vector_failure_returns_500():
    app, client = _build_client_with_db()
    _seed_products(app)
    app.config['PRODUCT_SEARCH_SERVICE'] = FakeSearchService(
        fail=VectorSearchError('vector failed')
    )

    response = client.post(
        '/api/products/search',
        data={'image': (io.BytesIO(b'img'), 'a.jpg')},
        content_type='multipart/form-data',
    )

    assert response.status_code == 500
    body = response.get_json()
    assert body['error_code'] == 'VECTOR_SEARCH_ERROR'


def test_search_returns_service_results_verbatim():
    """去重已下沉到 SQL（VectorSearchService 内的 DISTINCT ON），
    端点不再做应用层折叠。折叠正确性由 test/integration/test_vector_search.py 覆盖。
    """
    app, client = _build_client_with_db()
    _seed_products(app)
    app.config['PRODUCT_SEARCH_SERVICE'] = FakeSearchService(
        results=[
            {'model_number': 'M-001', 'image_path': '/uploads/a.jpg', 'similarity': 0.95},
            {'model_number': 'M-002', 'image_path': '/uploads/c.jpg', 'similarity': 0.75},
        ]
    )

    response = client.post(
        '/api/products/search',
        data={'image': (io.BytesIO(b'img'), 'a.jpg'), 'top_k': '10'},
        content_type='multipart/form-data',
    )

    assert response.status_code == 200
    body = response.get_json()
    assert len(body) == 2
    assert body[0]['model_number'] == 'M-001'
    assert body[0]['matched_image'] == '/uploads/a.jpg'
    assert body[0]['similarity'] == 0.95
    assert body[1]['model_number'] == 'M-002'


def test_search_preserves_service_result_order():
    """端点必须保持服务返回的顺序（已按距离升序），不得被字典查询打乱。"""
    app, client = _build_client_with_db()
    _seed_products(app)
    app.config['PRODUCT_SEARCH_SERVICE'] = FakeSearchService(
        results=[
            {'model_number': 'M-002', 'image_path': '/uploads/c.jpg', 'similarity': 0.91},
            {'model_number': 'M-001', 'image_path': '/uploads/a.jpg', 'similarity': 0.42},
        ]
    )

    response = client.post(
        '/api/products/search',
        data={'image': (io.BytesIO(b'img'), 'a.jpg'), 'top_k': '10'},
        content_type='multipart/form-data',
    )

    body = response.get_json()
    assert [item['model_number'] for item in body] == ['M-002', 'M-001']


def test_create_product_embedding_failure_rolls_back():
    app, client = _build_client_with_db()
    app.config['PRODUCT_SEARCH_SERVICE'] = FakeCreateService(
        exc=EmbeddingServiceError('cannot embed')
    )

    payload = {
        'product': json.dumps({
            'model_number': 'ROLLBACK-001',
            'photographer_file': 'pf',
            'alibaba_product_url': 'https://example.com/item',
            'category': 'cat',
        }),
        'images': (io.BytesIO(b'img'), 'a.jpg'),
    }

    response = client.post('/api/products', data=payload, content_type='multipart/form-data')

    assert response.status_code == 503
    body = response.get_json()
    assert body['error_code'] == 'EMBEDDING_SERVICE_ERROR'

    with app.app_context():
        assert Product.query.filter_by(model_number='ROLLBACK-001').first() is None
        assert ProductImage.query.filter_by(model_number='ROLLBACK-001').first() is None
