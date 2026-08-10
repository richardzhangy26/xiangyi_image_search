"""Issue #21 worker 取消与迟到结果防护的纯单元测试（伪仓库/伪 session）。"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace
import uuid

from services.embedding import EMBEDDING_DIMENSION, EMBEDDING_MODEL, EmbeddingResult
from services.image_import_worker import (
    ClaimedImportItem,
    ImageImportWorker,
    complete_import_item,
    mark_import_item_failed,
    sweep_cancelled_imports,
)
from models import AssetActivityRecord, ImageAsset


def _claim():
    return ClaimedImportItem(
        item_id=uuid.uuid4(),
        claim_token=uuid.uuid4(),
        claim_generation=1,
        attempt_count=1,
        request_id='request-21',
        source_provider='image-import-upload',
        source_bucket='image-imports',
        source_relative_path='imports/hash/0001/item.png',
        source_revision=1,
        display_name='item.png',
        oss_path='private/original',
        preview_oss_path='private/preview',
        content_hash='f' * 64,
        source_size=123,
        source_mime_type='image/png',
        source_width=40,
        source_height=24,
        normalization_version='preview-v1',
        expected_embedding_model=EMBEDDING_MODEL,
        expected_embedding_dimension=EMBEDDING_DIMENSION,
        created_at=datetime(2026, 8, 10, 12, 0, 0),
    )


class CancelFakeRepository:
    def __init__(self, claim, events, *, cancel_requested=False):
        self.claim = claim
        self.events = events
        self.cancel_requested = cancel_requested
        self.completed = []
        self.failed = []
        self.cancelled = []

    def claim_next(self, *, worker_id, lease_seconds):
        self.events.append(('claim', worker_id, lease_seconds))
        return self.claim

    def cancel_if_requested(self, claim):
        self.events.append(('cancel_check', claim.item_id))
        if self.cancel_requested:
            self.cancelled.append(claim)
            return True
        return False

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
    def __init__(self, events, result):
        self.events = events
        self.result = result

    def embed_normalized_image_result(self, image_path, request_id=None):
        self.events.append(('embedding', request_id))
        return self.result


def _worker(repository, storage, embedding):
    return ImageImportWorker(
        repository=repository,
        storage=storage,
        embedding_client=embedding,
        worker_id='worker-21',
        lease_seconds=300,
    )


def test_cancel_intent_before_embedding_call_skips_download_and_embedding():
    events = []
    repository = CancelFakeRepository(_claim(), events, cancel_requested=True)
    result = EmbeddingResult(model=EMBEDDING_MODEL, vector=[0.1] * EMBEDDING_DIMENSION)
    worker = _worker(repository, FakeStorage(events), FakeEmbedding(events, result))

    assert worker.process_one() is True
    assert [event[0] for event in events] == ['claim', 'cancel_check']
    assert repository.completed == []
    assert repository.failed == []
    assert len(repository.cancelled) == 1


def test_no_cancel_intent_proceeds_to_embedding_and_completion():
    events = []
    repository = CancelFakeRepository(_claim(), events, cancel_requested=False)
    result = EmbeddingResult(model=EMBEDDING_MODEL, vector=[0.1] * EMBEDDING_DIMENSION)
    worker = _worker(repository, FakeStorage(events), FakeEmbedding(events, result))

    assert worker.process_one() is True
    assert [event[0] for event in events] == [
        'claim', 'cancel_check', 'download', 'embedding', 'complete'
    ]
    assert len(repository.completed) == 1


class _ScalarResult:
    def __init__(self, item):
        self.item = item

    def scalar_one_or_none(self):
        return self.item


class _CancelSession:
    def __init__(self, item):
        self.item = item
        self.execute_count = 0
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, _statement):
        # 首次 execute 返回被锁定的任务行；后续（来源资产查询）返回 None，
        # 使 complete 走新建正式资产路径。
        self.execute_count += 1
        return _ScalarResult(self.item if self.execute_count == 1 else None)

    def begin_nested(self):
        return nullcontext()

    def add(self, value):
        self.added.append(value)

    def flush(self):
        for value in self.added:
            if isinstance(value, ImageAsset) and value.id is None:
                value.id = uuid.uuid4()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _embedding_item(claim, *, cancel_requested_at=None):
    return SimpleNamespace(
        id=claim.item_id,
        status='embedding',
        claim_token=claim.claim_token,
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
        asset_id=None,
        cancel_requested_at=cancel_requested_at,
        cancel_requested_by='api',
        cancelled_at=None,
        completed_at=None,
        failed_at=None,
        failure_message=None,
        claimed_by='worker-21',
        claimed_at=datetime(2026, 8, 10, 12, 0, 0),
        lease_expires_at=datetime(2026, 8, 10, 12, 5, 0),
        updated_at=datetime(2026, 8, 10, 12, 0, 0),
    )


def test_complete_discards_late_result_when_cancel_intent_exists():
    claim = _claim()
    item = _embedding_item(claim, cancel_requested_at=datetime(2026, 8, 10, 12, 1, 0))
    session = _CancelSession(item)

    result = complete_import_item(session, claim, [0.2] * EMBEDDING_DIMENSION)

    assert result == 'discarded'
    assert item.status == 'cancelled'
    assert item.asset_id is None
    assert item.cancelled_at is not None
    assert session.added and not any(
        isinstance(value, ImageAsset) for value in session.added
    )
    activities = [
        record for record in session.added
        if isinstance(record, AssetActivityRecord)
    ]
    assert any(
        record.event_type == 'image_import.late_result_discarded'
        for record in activities
    )
    assert session.commits == 1


def test_complete_creates_asset_when_no_cancel_intent():
    claim = _claim()
    item = _embedding_item(claim, cancel_requested_at=None)
    session = _CancelSession(item)

    result = complete_import_item(session, claim, [0.2] * EMBEDDING_DIMENSION)

    assert result is True
    assert item.status == 'completed'
    assert any(isinstance(value, ImageAsset) for value in session.added)


def test_cancel_if_requested_transitions_when_intent_present():
    from services.image_import_worker import cancel_import_item_if_requested

    claim = _claim()
    item = _embedding_item(claim, cancel_requested_at=datetime(2026, 8, 10, 12, 1, 0))
    session = _CancelSession(item)

    assert cancel_import_item_if_requested(session, claim) is True
    assert item.status == 'cancelled'
    assert item.cancelled_at is not None
    assert item.claim_token is None
    assert session.commits == 1
    activities = [
        record for record in session.added
        if isinstance(record, AssetActivityRecord)
    ]
    assert any(
        record.event_type == 'image_import.cancelled' for record in activities
    )


def test_cancel_if_requested_returns_false_without_intent():
    from services.image_import_worker import cancel_import_item_if_requested

    claim = _claim()
    item = _embedding_item(claim, cancel_requested_at=None)
    session = _CancelSession(item)

    assert cancel_import_item_if_requested(session, claim) is False
    assert item.status == 'embedding'
    assert session.commits == 0


def test_failed_transition_yields_cancelled_when_intent_present():
    claim = _claim()
    item = _embedding_item(claim, cancel_requested_at=datetime(2026, 8, 10, 12, 1, 0))
    session = _CancelSession(item)

    assert mark_import_item_failed(session, claim, '处理失败（RuntimeError）') is True
    assert item.status == 'cancelled'
    assert item.asset_id is None


class _SweepSession:
    def __init__(self, rows):
        self.rows = rows
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, _statement):
        class _Multi:
            def __init__(self, items):
                self._items = items

            def scalars(self):
                return self

            def all(self):
                return self._items

        return _Multi(self.rows)

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_sweep_transitions_expired_lease_intent_rows_to_cancelled():
    claim = _claim()
    stale = _embedding_item(
        claim, cancel_requested_at=datetime(2026, 8, 10, 12, 1, 0)
    )
    stale.lease_expires_at = datetime(2026, 8, 10, 11, 0, 0)
    session = _SweepSession([stale])

    count = sweep_cancelled_imports(session, now=datetime(2026, 8, 10, 12, 30, 0))

    assert count == 1
    assert stale.status == 'cancelled'
    assert stale.cancelled_at is not None
    assert session.commits == 1
