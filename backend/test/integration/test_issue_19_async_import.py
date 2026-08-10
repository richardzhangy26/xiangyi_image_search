"""Issue #19 真实 PostgreSQL 并发与异步闭环场景。

只使用 fake OSS / fake embedding，但要求本地隔离 PostgreSQL。当前 Ticket 未获
执行授权，因此当前验收不得收集或运行本文件。
"""

from __future__ import annotations

import hashlib
import io
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from models import ImageAsset, ImageImportItem, db
from services.embedding import EMBEDDING_DIMENSION, EMBEDDING_MODEL, EmbeddingResult
from services.image_import_worker import (
    ImageImportWorker,
    SqlAlchemyImageImportRepository,
    claim_next_import_item,
    complete_import_item,
)
from services.object_storage import StoredObject
from services.vector_search import VectorSearchService


def _task(path, request_id):
    digest = hashlib.sha256(path.encode()).hexdigest()
    return ImageImportItem(
        source_provider='image-import-upload', source_bucket='image-imports',
        source_relative_path=path, source_revision=1,
        display_name=path.rsplit('/', 1)[-1],
        oss_path=f'private/original/{digest}',
        preview_oss_path=f'private/preview/{digest}', content_hash=digest,
        source_size=10, source_mime_type='image/png', source_width=2,
        source_height=2, normalization_version='preview-v1',
        expected_embedding_model=EMBEDDING_MODEL,
        expected_embedding_dimension=EMBEDDING_DIMENSION,
        status='queued', request_id=request_id,
    )


def _independent_session_factory(schema_name):
    engine = create_engine(os.environ['DATABASE_URL'])
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def open_session():
        session = factory()
        session.execute(text(f'SET search_path TO "{schema_name}", public'))
        session.commit()
        return session

    return engine, open_session


def test_two_workers_claim_different_items_with_skip_locked(app):
    db.session.add_all([
        _task('imports/a/0001/a.png', 'concurrency-a'),
        _task('imports/b/0001/b.png', 'concurrency-b'),
    ])
    db.session.commit()
    schema_name = db.session.execute(text('SELECT current_schema()')).scalar_one()
    db.session.rollback()
    engine, open_session = _independent_session_factory(schema_name)

    def claim(worker_id):
        session = open_session()
        try:
            return claim_next_import_item(session, worker_id=worker_id)
        finally:
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, ('worker-a', 'worker-b')))
        assert all(claims)
        assert len({claim.item_id for claim in claims}) == 2
    finally:
        engine.dispose()


def test_expired_lease_takeover_fences_the_stale_claim(app):
    db.session.add(_task('imports/lease/0001/item.png', 'lease-19'))
    db.session.commit()
    first_now = datetime(2026, 8, 9, 12, 0, 0)
    first = claim_next_import_item(
        db.session, worker_id='worker-first', lease_seconds=60, now=first_now
    )
    assert claim_next_import_item(
        db.session,
        worker_id='worker-early',
        lease_seconds=60,
        now=first_now + timedelta(seconds=30),
    ) is None
    second = claim_next_import_item(
        db.session,
        worker_id='worker-takeover',
        lease_seconds=60,
        now=first_now + timedelta(seconds=61),
    )

    vector = [0.1] * EMBEDDING_DIMENSION
    assert complete_import_item(db.session, first, vector) is False
    assert complete_import_item(db.session, second, vector) is True
    assert ImageAsset.query.count() == 1


class _MemoryStorage:
    def __init__(self):
        self.objects = {}
        self.specs = {}

    def head_object(self, key):
        if key not in self.objects:
            return None
        spec = self.specs[key]
        return StoredObject(
            key=key, size=len(self.objects[key]), content_type=spec.content_type,
            metadata=spec.metadata, etag=spec.md5_hex,
        )

    def put_file(self, key, source_path, *, spec):
        with open(source_path, 'rb') as source:
            self.objects[key] = source.read()
        self.specs[key] = spec

    def put_bytes(self, key, data, *, spec):
        self.objects[key] = bytes(data)
        self.specs[key] = spec

    def download_file(self, key, target_path):
        target_path.write_bytes(self.objects[key])


class _FakeEmbedding:
    def embed_normalized_image_result(self, image_path, request_id=None):
        return EmbeddingResult(
            model=EMBEDDING_MODEL,
            vector=[0.1] * EMBEDDING_DIMENSION,
        )


def test_http_queue_worker_completion_and_vector_discovery(app):
    storage = _MemoryStorage()
    app.config['IMAGE_ASSET_STORAGE'] = storage
    client = app.test_client()
    source = io.BytesIO()
    Image.new('RGB', (8, 6), 'blue').save(source, format='PNG')

    queued = client.post(
        '/api/image-imports',
        data={'images': (io.BytesIO(source.getvalue()), '闭环.png')},
        content_type='multipart/form-data',
    )
    assert queued.status_code == 202
    assert ImageAsset.query.count() == 0

    worker = ImageImportWorker(
        repository=SqlAlchemyImageImportRepository(db.session),
        storage=storage,
        embedding_client=_FakeEmbedding(),
        worker_id='integration-worker',
    )
    assert worker.process_until_idle() == 1

    item_id = queued.get_json()['items'][0]['item_id']
    persisted = client.get(f'/api/image-imports/{item_id}').get_json()
    assert persisted['status'] == 'completed'
    asset = ImageAsset.query.one()
    assert asset.model_number is None
    assert asset.status == 'active'
    assert VectorSearchService().search_by_vector(
        [0.1] * EMBEDDING_DIMENSION,
        top_k=1,
    )[0]['asset_id'] == str(asset.id)

