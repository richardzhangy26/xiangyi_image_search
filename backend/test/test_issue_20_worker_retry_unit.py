"""Issue #20 worker 重试链路的纯单元测试（伪时间/伪仓库/伪 embedding）。"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace
import uuid

import pytest

from services import import_retry
from services.embedding import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EmbeddingNetworkError,
    EmbeddingRateLimitExhaustedError,
    EmbeddingResult,
    EmbeddingServiceError,
)
from services.image_import_worker import (
    ClaimedImportItem,
    ImageImportWorker,
    claim_next_import_item,
    mark_import_item_failed,
    schedule_import_retry,
)
from models import AssetActivityRecord


def _claim(attempt_count=1):
    return ClaimedImportItem(
        item_id=uuid.uuid4(),
        claim_token=uuid.uuid4(),
        claim_generation=2,
        attempt_count=attempt_count,
        request_id='request-20',
        source_provider='image-import-upload',
        source_bucket='image-imports',
        source_relative_path='imports/hash/0001/item.png',
        source_revision=1,
        display_name='item.png',
        oss_path='private/original',
        preview_oss_path='private/preview',
        content_hash='c' * 64,
        source_size=123,
        source_mime_type='image/png',
        source_width=40,
        source_height=24,
        normalization_version='preview-v1',
        expected_embedding_model=EMBEDDING_MODEL,
        expected_embedding_dimension=EMBEDDING_DIMENSION,
        created_at=datetime(2026, 8, 10, 12, 0, 0),
    )


class RetryFakeRepository:
    def __init__(self, claim, events):
        self.claim = claim
        self.events = events
        self.completed = []
        self.failed = []
        self.retries = []

    def claim_next(self, *, worker_id, lease_seconds):
        self.events.append(('claim', worker_id, lease_seconds))
        return self.claim

    def complete(self, claim, vector):
        self.events.append(('complete', claim.item_id))
        self.completed.append((claim, vector))
        return True

    def fail(self, claim, failure_message, error_class=None):
        self.events.append(('fail', claim.item_id))
        self.failed.append((claim, failure_message, error_class))
        return True

    def schedule_retry(self, claim, *, error_class, failure_message):
        self.events.append(('schedule_retry', claim.item_id))
        self.retries.append((claim, error_class, failure_message))
        return True

    def cancel_if_requested(self, claim):
        return False

    def sweep_cancelled(self):
        return 0

    def queue_depth(self):
        return 0


class FakeStorage:
    def __init__(self, events, error=None):
        self.events = events
        self.error = error

    def download_file(self, key, target_path):
        self.events.append(('download', key))
        if self.error:
            raise self.error
        target_path.write_bytes(b'private-preview')


class FakeEmbedding:
    def __init__(self, events, result=None, error=None):
        self.events = events
        self.result = result
        self.error = error

    def embed_normalized_image_result(self, image_path, request_id=None):
        self.events.append(('embedding', request_id))
        if self.error:
            raise self.error
        return self.result


def _worker(repository, storage, embedding):
    return ImageImportWorker(
        repository=repository,
        storage=storage,
        embedding_client=embedding,
        worker_id='worker-20',
        lease_seconds=300,
    )


def test_rate_limited_first_attempt_schedules_retry_without_failing():
    events = []
    repository = RetryFakeRepository(_claim(attempt_count=1), events)
    worker = _worker(
        repository,
        FakeStorage(events),
        FakeEmbedding(
            events, error=EmbeddingRateLimitExhaustedError('429重试3次后仍限流')
        ),
    )

    assert worker.process_one() is True
    assert repository.completed == []
    assert repository.failed == []
    assert len(repository.retries) == 1
    _, error_class, message = repository.retries[0]
    assert error_class == 'rate_limited'
    assert message == '处理失败（EmbeddingRateLimitExhaustedError）'
    assert '429重试' not in message


def test_network_error_within_budget_schedules_retry():
    events = []
    repository = RetryFakeRepository(_claim(attempt_count=4), events)
    worker = _worker(
        repository,
        FakeStorage(events),
        FakeEmbedding(events, error=EmbeddingNetworkError('图片向量提取失败: 超时')),
    )

    assert worker.process_one() is True
    assert repository.failed == []
    assert len(repository.retries) == 1
    assert repository.retries[0][1] == 'network'


def test_retryable_error_at_budget_limit_is_stable_failure():
    events = []
    repository = RetryFakeRepository(
        _claim(attempt_count=import_retry.MAX_AUTO_ATTEMPTS), events
    )
    worker = _worker(
        repository,
        FakeStorage(events),
        FakeEmbedding(
            events, error=EmbeddingRateLimitExhaustedError('429重试3次后仍限流')
        ),
    )

    assert worker.process_one() is True
    assert repository.retries == []
    assert len(repository.failed) == 1
    claim, message, error_class = repository.failed[0]
    assert error_class == 'rate_limited'
    assert message == '处理失败（EmbeddingRateLimitExhaustedError）'


def test_incompatible_embedding_result_is_deterministic_failure():
    events = []
    repository = RetryFakeRepository(_claim(attempt_count=1), events)
    worker = _worker(
        repository,
        FakeStorage(events),
        FakeEmbedding(
            events,
            result=EmbeddingResult(
                model=EMBEDDING_MODEL, vector=[0.1] * (EMBEDDING_DIMENSION - 1)
            ),
        ),
    )

    assert worker.process_one() is True
    assert repository.retries == []
    assert len(repository.failed) == 1
    assert repository.failed[0][2] == 'embedding_incompatible'


def test_bad_image_request_error_is_deterministic_failure():
    events = []
    repository = RetryFakeRepository(_claim(attempt_count=1), events)
    worker = _worker(
        repository,
        FakeStorage(events),
        FakeEmbedding(
            events,
            error=EmbeddingServiceError('标准化搜索预览图无法解码: BadImage'),
        ),
    )

    assert worker.process_one() is True
    assert repository.retries == []
    assert repository.failed[0][2] == 'deterministic_request'


def test_unknown_exception_never_consumes_retry_budget_automatically():
    events = []
    repository = RetryFakeRepository(_claim(attempt_count=1), events)
    worker = _worker(
        repository,
        FakeStorage(events),
        FakeEmbedding(
            events,
            error=RuntimeError('message mentioning 429 503 network timeout'),
        ),
    )

    assert worker.process_one() is True
    assert repository.retries == []
    assert repository.failed[0][2] == 'unknown'
    assert repository.failed[0][1] == '处理失败（RuntimeError）'
    assert 'network timeout' not in repository.failed[0][1]


def test_transient_preview_download_failure_schedules_retry():
    events = []
    repository = RetryFakeRepository(_claim(attempt_count=2), events)
    storage_error = RuntimeError('connection reset')
    from services.object_storage import ObjectStorageError

    download_error = ObjectStorageError('OSS 下载失败: ConnectionResetError')
    download_error.stage = 'download'
    worker = _worker(
        repository,
        FakeStorage(events, error=download_error),
        FakeEmbedding(events),
    )
    assert storage_error is not None

    assert worker.process_one() is True
    assert repository.failed == []
    assert repository.retries[0][1] == 'transient_storage'


def test_missing_preview_object_is_deterministic_failure():
    from services.object_storage import ObjectStorageError

    events = []
    repository = RetryFakeRepository(_claim(attempt_count=1), events)
    download_error = ObjectStorageError('OSS 下载失败: NoSuchKey')
    download_error.stage = 'download'
    download_error.status_code = 404
    worker = _worker(
        repository,
        FakeStorage(events, error=download_error),
        FakeEmbedding(events),
    )

    assert worker.process_one() is True
    assert repository.retries == []
    assert repository.failed[0][2] == 'storage_missing'


class _ScalarResult:
    def __init__(self, item):
        self.item = item

    def scalar_one_or_none(self):
        return self.item


class _RetrySession:
    def __init__(self, item):
        self.item = item
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, _statement):
        return _ScalarResult(self.item)

    def begin_nested(self):
        return nullcontext()

    def add(self, value):
        self.added.append(value)

    def flush(self):
        return None

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _retry_item(claim, *, status='embedding', claim_token=None,
                cancel_requested_at=None):
    return SimpleNamespace(
        id=claim.item_id,
        status=status,
        claim_token=claim_token or claim.claim_token,
        claim_generation=claim.claim_generation,
        attempt_count=claim.attempt_count,
        request_id=claim.request_id,
        last_error_class=None,
        last_attempt_at=None,
        next_retry_at=None,
        cancel_requested_at=cancel_requested_at,
        failed_at=None,
        failure_message=None,
        claimed_by='worker-20',
        claimed_at=datetime(2026, 8, 10, 12, 0, 0),
        lease_expires_at=datetime(2026, 8, 10, 12, 5, 0),
        updated_at=datetime(2026, 8, 10, 12, 0, 0),
    )


def test_schedule_retry_persists_awaiting_retry_with_backoff_and_activity(monkeypatch):
    monkeypatch.delenv('IMAGE_IMPORT_RETRY_BASE_SECONDS', raising=False)
    monkeypatch.delenv('IMAGE_IMPORT_RETRY_CAP_SECONDS', raising=False)
    claim = _claim(attempt_count=2)
    item = _retry_item(claim)
    session = _RetrySession(item)
    now = datetime(2026, 8, 10, 12, 10, 0)

    assert schedule_import_retry(
        session,
        claim,
        error_class='rate_limited',
        failure_message='处理失败（EmbeddingRateLimitExhaustedError）',
        now=now,
    ) is True

    assert item.status == 'awaiting_retry'
    assert item.attempt_count == 2
    assert item.last_error_class == 'rate_limited'
    assert item.failure_message == '处理失败（EmbeddingRateLimitExhaustedError）'
    assert item.next_retry_at == now + timedelta(seconds=60)
    assert item.failed_at is None
    assert item.claim_token is None
    assert item.claimed_by is None
    assert item.lease_expires_at is None
    assert session.commits == 1
    assert session.rollbacks == 0
    activities = [
        record for record in session.added
        if isinstance(record, AssetActivityRecord)
    ]
    assert len(activities) == 1
    assert activities[0].event_type == 'image_import.awaiting_retry'
    assert activities[0].after_state['status'] == 'awaiting_retry'
    assert activities[0].after_state['error_class'] == 'rate_limited'


def test_schedule_retry_with_stale_claim_token_returns_false():
    claim = _claim(attempt_count=1)
    item = _retry_item(claim, claim_token=uuid.uuid4())
    session = _RetrySession(item)

    assert schedule_import_retry(
        session,
        claim,
        error_class='network',
        failure_message='处理失败（EmbeddingNetworkError）',
    ) is False
    assert session.commits == 0
    assert session.rollbacks == 1
    assert item.status == 'embedding'


def test_failed_transition_persists_error_class(monkeypatch):
    claim = _claim(attempt_count=1)
    item = _retry_item(claim)
    session = _RetrySession(item)

    assert mark_import_item_failed(
        session,
        claim,
        '处理失败（RuntimeError）',
        error_class='unknown',
    ) is True

    assert item.status == 'failed'
    assert item.last_error_class == 'unknown'
    assert session.commits == 1


class _ClaimableItem(SimpleNamespace):
    pass


class _ClaimSession:
    def __init__(self, item):
        self.item = item
        self.commits = 0
        self.rollbacks = 0

    def execute(self, _statement):
        return _ScalarResult(self.item)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_claim_counts_attempt_and_clears_schedule_before_snapshot():
    item = _ClaimableItem(
        id=uuid.uuid4(),
        claim_token=None,
        claim_generation=1,
        attempt_count=2,
        last_attempt_at=None,
        next_retry_at=datetime(2026, 8, 10, 11, 0, 0),
        request_id='request-claim-20',
        source_provider='image-import-upload',
        source_bucket='image-imports',
        source_relative_path='imports/hash/0001/item.png',
        source_revision=1,
        display_name='item.png',
        oss_path='private/original',
        preview_oss_path='private/preview',
        content_hash='d' * 64,
        source_size=50,
        source_mime_type='image/png',
        source_width=10,
        source_height=5,
        normalization_version='preview-v1',
        expected_embedding_model=EMBEDDING_MODEL,
        expected_embedding_dimension=EMBEDDING_DIMENSION,
        status='awaiting_retry',
        created_at=datetime(2026, 8, 10, 10, 0, 0),
    )
    session = _ClaimSession(item)

    claim = claim_next_import_item(
        session,
        worker_id='worker-claim-20',
        lease_seconds=300,
        now=datetime(2026, 8, 10, 12, 0, 0),
    )

    assert claim.attempt_count == 3
    assert claim.claim_generation == 2
    assert item.attempt_count == 3
    assert item.next_retry_at is None
    assert item.last_attempt_at == datetime(2026, 8, 10, 12, 0, 0)
    assert session.commits == 1


def test_schedule_retry_yields_to_cancel_intent(monkeypatch):
    """并集规则：已提交取消意图的任务不得再进入等待重试，直接转 cancelled。"""
    monkeypatch.delenv('IMAGE_IMPORT_RETRY_BASE_SECONDS', raising=False)
    monkeypatch.delenv('IMAGE_IMPORT_RETRY_CAP_SECONDS', raising=False)
    claim = _claim(attempt_count=1)
    item = _retry_item(
        claim, cancel_requested_at=datetime(2026, 8, 10, 12, 5, 0)
    )
    session = _RetrySession(item)

    assert schedule_import_retry(
        session,
        claim,
        error_class='network',
        failure_message='处理失败（EmbeddingNetworkError）',
    ) is True

    assert item.status == 'cancelled'
    assert item.next_retry_at is None
    assert item.cancelled_at is not None
    assert session.commits == 1
