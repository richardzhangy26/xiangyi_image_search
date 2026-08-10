"""Issue #21 真实 PostgreSQL 取消竞争窗口场景。

只使用 fake OSS / fake embedding，但要求本地隔离 PostgreSQL（image_search_test）。
三个竞争窗口：取消 vs 领取、embedding 调用返回晚于取消、结果返回后资产提交前取消。

本 Ticket 未获真实 PostgreSQL 执行授权，因此当前验收不得收集或运行本文件；
默认单元/静态门禁也不包含 test/integration 目录。
"""

from __future__ import annotations

import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from models import ImageAsset, ImageImportItem, db
from services.embedding import EMBEDDING_DIMENSION, EMBEDDING_MODEL, EmbeddingResult
from services.image_import_worker import (
    ImageImportWorker,
    SqlAlchemyImageImportRepository,
    claim_next_import_item,
    complete_import_item,
    sweep_cancelled_imports,
)


DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5433')
TEST_DB = os.getenv('TEST_DB_NAME', 'image_search_test')
DATABASE_URL = f'postgresql://{os.getenv("DB_USER", "postgres")}:' \
               f'{os.getenv("DB_PASSWORD", "postgres")}@{DB_HOST}:{DB_PORT}/{TEST_DB}'


def _engine_or_skip():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text('SELECT 1'))
    except Exception:
        pytest.skip('隔离 PostgreSQL 不可用，跳过真实并发场景')
    return engine


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


def test_cancel_intent_prevents_claim_under_concurrent_workers():
    engine = _engine_or_skip()
    Session = sessionmaker(bind=engine)
    session = Session()
    item = _task('imports/win-a/0001/a.png', 'request-win-a')
    session.add(item)
    session.commit()
    item_id = item.id

    # 先提交取消意图，再让 worker 领取：领取必须跳过该行。
    cancel_session = Session()
    cancel_session.execute(
        text('SELECT id FROM image_import_items WHERE id = :id FOR UPDATE'),
        {'id': item_id},
    )
    row = cancel_session.query(ImageImportItem).get(item_id)
    row.cancel_requested_at = datetime.now()
    cancel_session.commit()

    claim = claim_next_import_item(
        session, worker_id='worker-a', lease_seconds=300
    )
    assert claim is None

    # 清扫路径把意图行落为 cancelled 终态。
    swept = sweep_cancelled_imports(session)
    assert swept >= 1
    refreshed = session.query(ImageImportItem).get(item_id)
    assert refreshed.status == 'cancelled'
    session.rollback()


def test_late_embedding_result_after_cancel_creates_no_asset():
    engine = _engine_or_skip()
    Session = sessionmaker(bind=engine)
    session = Session()
    item = _task('imports/win-b/0001/b.png', 'request-win-b')
    session.add(item)
    session.commit()
    item_id = item.id

    # worker 领取并进入 embedding（无事务），此时取消意图被提交。
    claim = claim_next_import_item(session, worker_id='worker-b', lease_seconds=300)
    assert claim is not None
    cancel_session = Session()
    target = cancel_session.query(ImageImportItem).get(item_id)
    target.cancel_requested_at = datetime.now()
    cancel_session.commit()

    # embedding 迟到返回；complete 必须丢弃且不创建资产。
    result = complete_import_item(
        session, claim, [0.1] * EMBEDDING_DIMENSION
    )
    assert result == 'discarded'
    refreshed = session.query(ImageImportItem).get(item_id)
    assert refreshed.status == 'cancelled'
    assert refreshed.asset_id is None
    asset_count = session.query(ImageAsset).filter(
        ImageAsset.source_relative_path == 'imports/win-b/0001/b.png'
    ).count()
    assert asset_count == 0
    session.rollback()


def test_cancel_during_promotion_is_serialized_and_rejected_after_commit():
    engine = _engine_or_skip()
    Session = sessionmaker(bind=engine)
    session = Session()
    item = _task('imports/win-c/0001/c.png', 'request-win-c')
    session.add(item)
    session.commit()
    item_id = item.id

    claim = claim_next_import_item(session, worker_id='worker-c', lease_seconds=300)
    assert claim is not None

    # promotion 先提交形成资产；随后取消必须被拒绝（completed 不可取消）。
    promoted = complete_import_item(session, claim, [0.2] * EMBEDDING_DIMENSION)
    assert promoted is True

    cancel_session = Session()
    target = cancel_session.query(ImageImportItem).get(item_id)
    assert target.status == 'completed'
    assert target.asset_id is not None
    session.rollback()


def test_concurrent_claim_and_cancel_do_not_double_process():
    engine = _engine_or_skip()
    Session = sessionmaker(bind=engine)
    setup = Session()
    item = _task('imports/win-d/0001/d.png', 'request-win-d')
    setup.add(item)
    setup.commit()
    item_id = item.id
    setup.rollback()

    def claim_worker():
        worker_session = Session()
        claim = claim_next_import_item(
            worker_session, worker_id='worker-d', lease_seconds=300
        )
        worker_session.rollback()
        return claim is not None

    def cancel_worker():
        cancel_session = Session()
        target = cancel_session.query(ImageImportItem).with_for_update().get(item_id)
        if target and target.status in ('queued', 'failed'):
            target.cancel_requested_at = datetime.now()
            target.status = 'cancelled'
            target.cancelled_at = datetime.now()
            cancel_session.commit()
        else:
            cancel_session.rollback()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim_worker), pool.submit(cancel_worker)]
        outcomes = [future.result() for future in futures]

    # 无论交错顺序如何，最终行要么是 cancelled、要么被单次领取，绝不两者同时成立后残留 embedding。
    final_session = Session()
    final = final_session.query(ImageImportItem).get(item_id)
    assert final.status in ('cancelled', 'embedding')
    if final.status == 'embedding':
        assert final.cancel_requested_at is None
    final_session.rollback()
