"""Issue #19 worker 的纯单元事务接缝与成功/失败合同。"""

from __future__ import annotations

from datetime import datetime
from contextlib import nullcontext
from types import SimpleNamespace
import uuid

import pytest

from services.embedding import EMBEDDING_DIMENSION, EMBEDDING_MODEL, EmbeddingResult
from services.image_import_worker import (
    ClaimedImportItem,
    ImageImportWorker,
    InvalidEmbeddingResult,
    claim_next_import_item,
    complete_import_item,
    validate_embedding_result,
)
from models import AssetActivityRecord, ImageAsset


def _claim():
    return ClaimedImportItem(
        item_id=uuid.uuid4(),
        claim_token=uuid.uuid4(),
        claim_generation=3,
        attempt_count=1,
        request_id='request-19',
        source_provider='image-import-upload',
        source_bucket='image-imports',
        source_relative_path='imports/hash/0001/item.png',
        source_revision=1,
        display_name='item.png',
        oss_path='private/original',
        preview_oss_path='private/preview',
        content_hash='a' * 64,
        source_size=123,
        source_mime_type='image/png',
        source_width=40,
        source_height=24,
        normalization_version='preview-v1',
        expected_embedding_model=EMBEDDING_MODEL,
        expected_embedding_dimension=EMBEDDING_DIMENSION,
        created_at=datetime(2026, 8, 9, 12, 0, 0),
    )


class FakeRepository:
    def __init__(self, claim, events):
        self.claim = claim
        self.events = events
        self.completed = []
        self.failed = []
        self.transaction_open = False

    def claim_next(self, *, worker_id, lease_seconds):
        self.events.append(('claim', worker_id, lease_seconds))
        return self.claim

    def complete(self, claim, vector):
        self.events.append(('complete', claim.item_id))
        self.completed.append((claim, vector))
        return True

    def fail(self, claim, failure_message, *, error_class=None):
        self.events.append(('fail', claim.item_id))
        self.failed.append((claim, failure_message))
        return True

    def schedule_retry(self, claim, *, error_class, failure_message):
        self.events.append(('schedule_retry', claim.item_id))
        return True

    def cancel_if_requested(self, claim):
        return False

    def sweep_cancelled(self):
        return 0

    def queue_depth(self):
        return 0


class FakeStorage:
    def __init__(self, events):
        self.events = events

    def download_file(self, key, target_path):
        self.events.append(('download', key))
        target_path.write_bytes(b'private-preview')


class FakeEmbedding:
    def __init__(self, repository, events, result=None, error=None):
        self.repository = repository
        self.events = events
        self.result = result
        self.error = error

    def embed_normalized_image_result(self, image_path, request_id=None):
        assert self.repository.transaction_open is False
        self.events.append(('embedding', request_id))
        if self.error:
            raise self.error
        return self.result


def test_success_claims_then_embeds_outside_transaction_then_completes():
    events = []
    claim = _claim()
    repository = FakeRepository(claim, events)
    result = EmbeddingResult(
        model=EMBEDDING_MODEL,
        vector=[0.25] * EMBEDDING_DIMENSION,
    )
    worker = ImageImportWorker(
        repository=repository,
        storage=FakeStorage(events),
        embedding_client=FakeEmbedding(repository, events, result=result),
        worker_id='worker-a',
        lease_seconds=300,
    )

    assert worker.process_one() is True
    assert [event[0] for event in events] == [
        'claim', 'download', 'embedding', 'complete'
    ]
    assert len(repository.completed) == 1
    assert repository.completed[0][1] == [0.25] * EMBEDDING_DIMENSION
    assert repository.failed == []


@pytest.mark.parametrize(
    'result',
    [
        EmbeddingResult(model='wrong-model', vector=[0.1] * EMBEDDING_DIMENSION),
        EmbeddingResult(model=EMBEDDING_MODEL, vector=[0.1] * 1023),
        EmbeddingResult(
            model=EMBEDDING_MODEL,
            vector=[0.1] * 1023 + [float('nan')],
        ),
        EmbeddingResult(
            model=EMBEDDING_MODEL,
            vector=[0.1] * 1023 + [float('inf')],
        ),
    ],
)
def test_invalid_model_dimension_nan_or_infinity_never_completes(result):
    with pytest.raises(InvalidEmbeddingResult):
        validate_embedding_result(result, claim=_claim())


def test_embedding_failure_marks_owned_item_failed_without_formal_asset():
    events = []
    claim = _claim()
    repository = FakeRepository(claim, events)
    worker = ImageImportWorker(
        repository=repository,
        storage=FakeStorage(events),
        embedding_client=FakeEmbedding(
            repository,
            events,
            error=RuntimeError('provider body must stay private'),
        ),
        worker_id='worker-a',
    )

    assert worker.process_one() is True
    assert repository.completed == []
    assert len(repository.failed) == 1
    assert repository.failed[0][1] == '处理失败（RuntimeError）'
    assert 'provider body' not in repository.failed[0][1]


def test_no_claim_returns_idle_without_touching_external_adapters():
    events = []
    repository = FakeRepository(None, events)
    worker = ImageImportWorker(
        repository=repository,
        storage=SimpleNamespace(),
        embedding_client=SimpleNamespace(),
        worker_id='worker-idle',
    )

    assert worker.process_one() is False
    assert events == [('claim', 'worker-idle', 300)]


class _ScalarResult:
    def __init__(self, item):
        self.item = item

    def scalar_one_or_none(self):
        return self.item


class _ClaimableItem(SimpleNamespace):
    def __getattribute__(self, name):
        if (
            name not in {'_expired', '__dict__', '__class__'}
            and object.__getattribute__(self, '__dict__').get('_expired', False)
        ):
            raise AssertionError('领取提交后不得再读取已过期 ORM 实例')
        return super().__getattribute__(name)


class _ClaimSession:
    def __init__(self, item):
        self.item = item
        self.commits = 0
        self.rollbacks = 0

    def execute(self, _statement):
        return _ScalarResult(self.item)

    def commit(self):
        self.commits += 1
        self.item._expired = True

    def rollback(self):
        self.rollbacks += 1


def test_claim_snapshots_before_commit_so_embedding_starts_without_reload_transaction():
    item = _ClaimableItem(
        id=uuid.uuid4(),
        claim_token=None,
        claim_generation=0,
        attempt_count=0,
        last_attempt_at=None,
        next_retry_at=None,
        request_id='request-claim',
        source_provider='image-import-upload',
        source_bucket='image-imports',
        source_relative_path='imports/hash/0001/item.png',
        source_revision=1,
        display_name='item.png',
        oss_path='private/original',
        preview_oss_path='private/preview',
        content_hash='b' * 64,
        source_size=50,
        source_mime_type='image/png',
        source_width=10,
        source_height=5,
        normalization_version='preview-v1',
        expected_embedding_model=EMBEDDING_MODEL,
        expected_embedding_dimension=EMBEDDING_DIMENSION,
        status='queued',
        created_at=datetime(2026, 8, 9, 10, 0, 0),
        _expired=False,
    )
    session = _ClaimSession(item)

    claim = claim_next_import_item(
        session,
        worker_id='worker-claim',
        lease_seconds=300,
        now=datetime(2026, 8, 9, 12, 0, 0),
    )

    assert claim.item_id == item.__dict__['id']
    assert claim.claim_generation == 1
    assert session.commits == 1
    assert session.rollbacks == 0


def test_observation_query_failure_cannot_turn_completed_work_into_failure():
    events = []
    claim = _claim()
    repository = FakeRepository(claim, events)
    repository.queue_depth = lambda: (_ for _ in ()).throw(
        RuntimeError('metrics unavailable')
    )
    worker = ImageImportWorker(
        repository=repository,
        storage=FakeStorage(events),
        embedding_client=FakeEmbedding(
            repository,
            events,
            result=EmbeddingResult(
                model=EMBEDDING_MODEL,
                vector=[0.25] * EMBEDDING_DIMENSION,
            ),
        ),
        worker_id='worker-observation',
    )

    assert worker.process_one() is True
    assert len(repository.completed) == 1
    assert repository.failed == []


def _promotion_item(claim, *, claim_token=None):
    return SimpleNamespace(
        id=claim.item_id,
        claim_token=claim_token or claim.claim_token,
        claim_generation=claim.claim_generation,
        request_id=claim.request_id,
        source_provider=claim.source_provider,
        source_bucket=claim.source_bucket,
        source_relative_path=claim.source_relative_path,
        source_revision=claim.source_revision,
        display_name=claim.display_name,
        oss_path=claim.oss_path,
        preview_oss_path=claim.preview_oss_path,
        content_hash=claim.content_hash,
        source_size=claim.source_size,
        source_mime_type=claim.source_mime_type,
        source_width=claim.source_width,
        source_height=claim.source_height,
        normalization_version=claim.normalization_version,
        expected_embedding_model=claim.expected_embedding_model,
        expected_embedding_dimension=claim.expected_embedding_dimension,
        status='embedding',
        asset_id=None,
        cancel_requested_at=None,
        completed_at=None,
        failed_at=None,
        failure_message=None,
        claimed_by='worker-a',
        claimed_at=datetime(2026, 8, 9, 12, 0, 0),
        lease_expires_at=datetime(2026, 8, 9, 12, 5, 0),
        updated_at=datetime(2026, 8, 9, 12, 0, 0),
    )


class _PromotionSession:
    def __init__(self, item, *, fail_stage=None):
        self.item = item
        self.fail_stage = fail_stage
        self.execute_count = 0
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.persisted_assets = []

    def execute(self, _statement):
        self.execute_count += 1
        return _ScalarResult(self.item if self.execute_count == 1 else None)

    def begin_nested(self):
        return nullcontext()

    def add(self, value):
        if self.fail_stage == 'activity' and isinstance(
            value, AssetActivityRecord
        ):
            raise RuntimeError('activity write failed')
        self.added.append(value)

    def flush(self):
        if self.fail_stage == 'flush':
            raise RuntimeError('asset flush failed')
        for value in self.added:
            if isinstance(value, ImageAsset) and value.id is None:
                value.id = uuid.uuid4()

    def commit(self):
        self.commits += 1
        if self.fail_stage == 'commit':
            raise RuntimeError('commit failed')
        self.persisted_assets = [
            value for value in self.added if isinstance(value, ImageAsset)
        ]

    def rollback(self):
        self.rollbacks += 1


def test_atomic_promotion_creates_one_active_unassigned_asset_and_completes():
    claim = _claim()
    item = _promotion_item(claim)
    session = _PromotionSession(item)

    assert complete_import_item(
        session,
        claim,
        [0.2] * EMBEDDING_DIMENSION,
    ) is True

    assert session.commits == 1
    assert session.rollbacks == 0
    assert len(session.persisted_assets) == 1
    asset = session.persisted_assets[0]
    assert asset.model_number is None
    assert asset.status == 'active'
    assert asset.embedding_model == EMBEDDING_MODEL
    assert asset.embedding_dimension == EMBEDDING_DIMENSION
    assert item.status == 'completed'
    assert item.asset_id == asset.id
    assert any(isinstance(value, AssetActivityRecord) for value in session.added)


@pytest.mark.parametrize('fail_stage', ['flush', 'activity', 'commit'])
def test_promotion_failure_rolls_back_without_a_formal_asset(fail_stage):
    claim = _claim()
    session = _PromotionSession(_promotion_item(claim), fail_stage=fail_stage)

    with pytest.raises(RuntimeError):
        complete_import_item(
            session,
            claim,
            [0.2] * EMBEDDING_DIMENSION,
        )

    assert session.persisted_assets == []
    assert session.rollbacks == 1


def test_stale_claim_token_cannot_promote_or_create_an_asset():
    claim = _claim()
    session = _PromotionSession(
        _promotion_item(claim, claim_token=uuid.uuid4())
    )

    assert complete_import_item(
        session,
        claim,
        [0.2] * EMBEDDING_DIMENSION,
    ) is False
    assert session.added == []
    assert session.commits == 0
    assert session.rollbacks == 1
