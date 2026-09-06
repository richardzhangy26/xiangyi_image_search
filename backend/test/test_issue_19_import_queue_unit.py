"""Issue #19 持久导入排队路径的纯单元测试。"""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

import services.asset_ingest as ingest_module
from services.asset_ingest import (
    AssetIngestConflictError,
    AssetIngestResult,
    ImageAssetIngestService,
    _PreparedAsset,
)


class ForbiddenEmbedding:
    def __getattr__(self, name):
        raise AssertionError(f'HTTP 排队路径不得访问 embedding: {name}')


class FakeImportItem:
    query = None

    def __init__(self, **values):
        self.id = 'import-item-19'
        for key, value in values.items():
            setattr(self, key, value)


class EmptyImportQuery:
    def filter_by(self, **_filters):
        return SimpleNamespace(one_or_none=lambda: None)


class ExistingImportQuery:
    def __init__(self, item):
        self.item = item

    def filter_by(self, **_filters):
        return SimpleNamespace(one_or_none=lambda: self.item)


class FakeSession:
    def __init__(self):
        self.added = []
        self.flushes = 0
        self.commits = 0
        self.rollbacks = 0

    def begin_nested(self):
        return nullcontext()

    def add(self, item):
        self.added.append(item)

    def flush(self):
        self.flushes += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _prepared():
    return _PreparedAsset(
        source_relative_path='imports/0/item.png',
        source_bucket='private-upload-bucket',
        model_number=None,
        content_hash='a' * 64,
        source_size=123,
        source_mime_type='image/png',
        source_width=40,
        source_height=24,
        normalization_version='preview-v1',
        oss_path='image-search/sources/product-upload/private/item.png',
        preview_oss_path='image-search/previews/preview-v1/aa/hash.jpg',
        preview_path=Path('/temporary/preview.jpg'),
        vector_values=None,
        stages={'download': 'new', 'original': 'new', 'preview': 'new'},
    )


def test_queue_one_persists_staged_metadata_without_embedding_or_asset(monkeypatch):
    session = FakeSession()
    FakeImportItem.query = EmptyImportQuery()
    monkeypatch.setattr(ingest_module, 'ImageImportItem', FakeImportItem, raising=False)
    monkeypatch.setattr(
        ingest_module,
        'db',
        SimpleNamespace(session=session),
    )

    service = ImageAssetIngestService(
        source=SimpleNamespace(
            resolve_location=lambda: SimpleNamespace(
                source_bucket='private-upload-bucket'
            )
        ),
        storage=SimpleNamespace(),
        embedding_client=ForbiddenEmbedding(),
        normalizer=SimpleNamespace(normalization_version='preview-v1'),
        source_provider='product-upload',
    )
    prepared = _prepared()
    monkeypatch.setattr(service, '_prepare_one', lambda *_args, **_kwargs: prepared)

    result = service.queue_one(
        prepared.source_relative_path,
        request_id='request-19',
        commit=True,
    )

    assert result.status == 'queued'
    assert result.item_id == 'import-item-19'
    assert result.asset_id is None
    assert result.source_relative_path == prepared.source_relative_path
    assert len(session.added) == 1
    item = session.added[0]
    assert item.status == 'queued'
    assert item.asset_id is None
    assert item.content_hash == prepared.content_hash
    assert item.expected_embedding_model == (
        'tongyi-embedding-vision-plus-2026-03-06'
    )
    assert item.expected_embedding_dimension == 1024
    assert session.flushes == 1
    assert session.commits == 1
    assert session.rollbacks == 0


def test_same_identity_and_content_returns_stable_task_and_closes_transaction(
    monkeypatch,
):
    session = FakeSession()
    existing = SimpleNamespace(
        id='existing-task-19',
        asset_id=None,
        content_hash='a' * 64,
    )
    FakeImportItem.query = ExistingImportQuery(existing)
    monkeypatch.setattr(ingest_module, 'ImageImportItem', FakeImportItem)
    monkeypatch.setattr(ingest_module, 'db', SimpleNamespace(session=session))
    service = ImageAssetIngestService(
        source=SimpleNamespace(),
        storage=SimpleNamespace(),
        embedding_client=ForbiddenEmbedding(),
        normalizer=SimpleNamespace(normalization_version='preview-v1'),
        source_provider='product-upload',
    )

    result = service._persist_import_item(
        _prepared(),
        request_id='request-existing',
        commit=True,
    )

    assert result.status == 'existing_task'
    assert result.item_id == 'existing-task-19'
    assert session.added == []
    assert session.commits == 1


def test_same_identity_with_different_content_is_an_explicit_conflict(monkeypatch):
    session = FakeSession()
    FakeImportItem.query = ExistingImportQuery(SimpleNamespace(
        id='existing-task-19', asset_id=None, content_hash='f' * 64,
    ))
    monkeypatch.setattr(ingest_module, 'ImageImportItem', FakeImportItem)
    monkeypatch.setattr(ingest_module, 'db', SimpleNamespace(session=session))
    service = ImageAssetIngestService(
        source=SimpleNamespace(), storage=SimpleNamespace(),
        embedding_client=ForbiddenEmbedding(),
        normalizer=SimpleNamespace(normalization_version='preview-v1'),
        source_provider='product-upload',
    )

    with pytest.raises(AssetIngestConflictError) as captured:
        service._persist_import_item(
            _prepared(), request_id='request-conflict', commit=True
        )

    assert captured.value.kind == 'source_conflict'
    assert session.added == []
    assert session.commits == 0


def test_archived_formal_asset_returns_recycle_bin_navigation_without_task(
    monkeypatch,
):
    session = FakeSession()
    monkeypatch.setattr(ingest_module, 'db', SimpleNamespace(session=session))
    service = ImageAssetIngestService(
        source=SimpleNamespace(
            resolve_location=lambda: SimpleNamespace(source_bucket='bucket')
        ),
        storage=SimpleNamespace(),
        embedding_client=ForbiddenEmbedding(),
        normalizer=SimpleNamespace(normalization_version='preview-v1'),
        source_provider='product-upload',
    )
    monkeypatch.setattr(service, '_prepare_one', lambda *_args, **_kwargs: (
        AssetIngestResult(
            status='in_recycle_bin',
            asset_id='archived-asset-19',
            content_hash='a' * 64,
            oss_path='private/original',
            preview_oss_path='private/preview',
            source_relative_path='imports/hash/0001/item.png',
            recovery_action={
                'type': 'open_recycle_bin',
                'asset_id': 'archived-asset-19',
            },
        )
    ))

    result = service.queue_one('imports/hash/0001/item.png', commit=True)

    assert result.status == 'in_recycle_bin'
    assert result.item_id is None
    assert result.recovery_action == {
        'type': 'open_recycle_bin',
        'asset_id': 'archived-asset-19',
    }
    assert session.added == []
    assert session.commits == 1
