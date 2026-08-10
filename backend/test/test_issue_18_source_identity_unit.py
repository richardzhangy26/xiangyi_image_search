"""Issue #18 来源身份判定的纯单元与 fake-adapter 场景。"""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

import services.asset_ingest as ingest_module
from services.asset_ingest import (
    AssetIngestConflictError,
    ImageAssetIngestService,
    _PreparedAsset,
)
from services.embedding import EMBEDDING_DIMENSION, EMBEDDING_MODEL
from services.object_source import SourceLocation, SourceObjectHead
from services.object_storage import (
    ObjectSpec,
    ObjectStorageConflictError,
    StoredObject,
)


SOURCE_BYTES = b'issue-18-source-image'
SOURCE_HASH = hashlib.sha256(SOURCE_BYTES).hexdigest()
SOURCE_MD5 = hashlib.md5(SOURCE_BYTES, usedforsecurity=False).hexdigest()
PREVIEW_BYTES = b'issue-18-preview'
PREVIEW_MD5 = hashlib.md5(PREVIEW_BYTES, usedforsecurity=False).hexdigest()


class FakeSource:
    def __init__(self, relative_path='catalog/item.png', data=SOURCE_BYTES):
        self.relative_path = relative_path
        self.data = data
        self.downloads = 0

    def resolve_location(self):
        return SourceLocation(
            source_bucket='source-bucket',
            s3_bucket='',
            s3_region='',
            endpoint_url='',
        )

    def head_object(self, key):
        assert key == self.relative_path
        return SourceObjectHead(key=key, size=len(self.data))

    def download_object(self, key, target, *, max_bytes=None):
        assert key == self.relative_path
        target.write(self.data)
        self.downloads += 1
        return len(self.data)


class ForbiddenEmbedding:
    def embed_normalized_image(self, *_args, **_kwargs):
        raise AssertionError('同源已有资产不得重新调用 embedding')


class ForbiddenNormalizer:
    normalization_version = 'preview-v1'

    def normalize(self, *_args, **_kwargs):
        raise AssertionError('同源已有资产不得重新生成预览')


class IdentityQuery:
    def __init__(self, identity_asset=None, reusable_asset=None):
        self.identity_asset = identity_asset
        self.reusable_asset = reusable_asset
        self.filters = []

    def filter_by(self, **filters):
        self.filters.append(filters)
        if 'source_provider' in filters:
            value = self.identity_asset
        else:
            value = self.reusable_asset
        return SimpleNamespace(
            one_or_none=lambda: value,
            first=lambda: value,
        )


class ExistingImageAssetModel:
    query = IdentityQuery()


def _existing_asset(*, status='active', content_hash=SOURCE_HASH):
    return SimpleNamespace(
        id='asset-stable-18',
        model_number='MODEL-18',
        source_provider='qiniu-kodo',
        source_bucket='source-bucket',
        source_relative_path='catalog/item.png',
        source_revision=1,
        display_name='item.png',
        version=7,
        oss_path='image-search/source-bucket/catalog/item.png',
        preview_oss_path=(
            f'image-search/previews/preview-v1/{SOURCE_HASH[:2]}/'
            f'{SOURCE_HASH}.jpg'
        ),
        content_hash=content_hash,
        source_size=len(SOURCE_BYTES),
        source_mime_type='image/png',
        source_width=40,
        source_height=24,
        vector=[0.1] * EMBEDDING_DIMENSION,
        embedding_model=EMBEDDING_MODEL,
        embedding_dimension=EMBEDDING_DIMENSION,
        normalization_version='preview-v1',
        status=status,
        archived_at='unchanged-archived-at' if status == 'archived' else None,
    )


class ExistingObjectStorage:
    def __init__(self, asset):
        self.asset = asset
        self.put_calls = []

    def head_object(self, key):
        if key == self.asset.oss_path:
            return StoredObject(
                key=key,
                size=len(SOURCE_BYTES),
                content_type='image/png',
                metadata={
                    'source-provider': 'qiniu-kodo',
                    'source-bucket': 'source-bucket',
                    'sha256': SOURCE_HASH,
                    'source-size': str(len(SOURCE_BYTES)),
                },
                etag=SOURCE_MD5,
            )
        if key == self.asset.preview_oss_path:
            return StoredObject(
                key=key,
                size=len(PREVIEW_BYTES),
                content_type='image/jpeg',
                metadata={
                    'sha256': SOURCE_HASH,
                    'normalization-version': 'preview-v1',
                    'preview-md5': PREVIEW_MD5,
                    'preview-size': str(len(PREVIEW_BYTES)),
                },
                etag=PREVIEW_MD5,
            )
        return None

    def put_file(self, key, source_path, *, spec):
        self.put_calls.append(key)

    def put_bytes(self, key, data, *, spec):
        self.put_calls.append(key)


def _service_for_existing(monkeypatch, asset):
    model = ExistingImageAssetModel
    model.query = IdentityQuery(identity_asset=asset)
    monkeypatch.setattr(ingest_module, 'ImageAsset', model)
    storage = ExistingObjectStorage(asset)
    service = ImageAssetIngestService(
        source=FakeSource(),
        storage=storage,
        embedding_client=ForbiddenEmbedding(),
        normalizer=ForbiddenNormalizer(),
    )
    return service, storage, model.query


def test_active_same_source_identity_and_content_is_stable_existing(monkeypatch):
    asset = _existing_asset(status='active')
    service, storage, query = _service_for_existing(monkeypatch, asset)

    result = service.ingest_one('catalog/item.png', commit=False)

    assert result.status == 'existing'
    assert result.asset_id == 'asset-stable-18'
    assert result.recovery_action is None
    assert storage.put_calls == []
    assert query.filters[0] == {
        'source_provider': 'qiniu-kodo',
        'source_bucket': 'source-bucket',
        'source_relative_path': 'catalog/item.png',
        'source_revision': 1,
    }


def test_archived_same_source_identity_returns_recycle_bin_without_restoring(
    monkeypatch,
):
    asset = _existing_asset(status='archived')
    before = vars(asset).copy()
    service, storage, _query = _service_for_existing(monkeypatch, asset)

    result = service.ingest_one('catalog/item.png', commit=False)

    assert result.status == 'in_recycle_bin'
    assert result.asset_id == 'asset-stable-18'
    assert result.recovery_action == {
        'type': 'open_recycle_bin',
        'asset_id': 'asset-stable-18',
    }
    assert vars(asset) == before
    assert storage.put_calls == []


def test_same_source_identity_with_changed_content_is_a_safe_conflict(monkeypatch):
    asset = _existing_asset(status='active', content_hash='0' * 64)
    service, storage, _query = _service_for_existing(monkeypatch, asset)

    with pytest.raises(AssetIngestConflictError) as captured:
        service.ingest_one('catalog/item.png', commit=False)

    assert captured.value.kind == 'source_conflict'
    assert captured.value.asset_id == 'asset-stable-18'
    assert captured.value.source_relative_path == 'catalog/item.png'
    assert storage.put_calls == []


class ConcurrentWinnerStorage:
    """模拟 HEAD 为空后，另一请求先完成同内容 PUT。"""

    def __init__(self, *, matching=True):
        self.matching = matching
        self.head_calls = 0
        self.put_calls = 0

    def head_object(self, key):
        self.head_calls += 1
        if self.head_calls == 1:
            return None
        metadata = {
            'source-provider': 'qiniu-kodo',
            'source-bucket': 'source-bucket',
            'sha256': SOURCE_HASH if self.matching else 'f' * 64,
            'source-size': str(len(SOURCE_BYTES)),
        }
        return StoredObject(
            key=key,
            size=len(SOURCE_BYTES),
            content_type='image/png',
            metadata=metadata,
            etag=SOURCE_MD5,
        )

    def put_file(self, key, source_path, *, spec):
        self.put_calls += 1
        raise ObjectStorageConflictError('concurrent winner')


@pytest.mark.parametrize('matching', [True, False])
def test_forbid_overwrite_race_requires_fresh_exact_head(tmp_path, matching):
    storage = ConcurrentWinnerStorage(matching=matching)
    source_path = tmp_path / 'source.png'
    source_path.write_bytes(SOURCE_BYTES)
    service = ImageAssetIngestService(
        source=FakeSource(),
        storage=storage,
        embedding_client=ForbiddenEmbedding(),
        normalizer=ForbiddenNormalizer(),
    )
    spec = ObjectSpec(
        size=len(SOURCE_BYTES),
        content_type='image/png',
        metadata={
            'source-provider': 'qiniu-kodo',
            'source-bucket': 'source-bucket',
            'sha256': SOURCE_HASH,
            'source-size': str(len(SOURCE_BYTES)),
        },
        md5_hex=SOURCE_MD5,
    )

    if matching:
        assert service._ensure_file_object(
            'original-key', source_path, spec=spec, conflict_name='原图'
        ) == 'reused'
    else:
        with pytest.raises(AssetIngestConflictError):
            service._ensure_file_object(
                'original-key', source_path, spec=spec, conflict_name='原图'
            )
    assert storage.head_calls == 2
    assert storage.put_calls == 1


@dataclass
class FakeCandidate:
    id: str = 'losing-candidate'

    def __init__(self, **values):
        for key, value in values.items():
            setattr(self, key, value)
        self.id = 'losing-candidate'


class RaceImageAssetModel(FakeCandidate):
    query = IdentityQuery()


class UniqueRaceSession:
    def __init__(self):
        self.added = []
        self.flush_calls = 0
        self.commits = 0
        self.rollbacks = 0

    def begin_nested(self):
        return nullcontext()

    def add(self, asset):
        self.added.append(asset)

    def flush(self):
        self.flush_calls += 1
        raise IntegrityError('insert image_assets', {}, Exception('unique'))

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _prepared_asset():
    return _PreparedAsset(
        source_relative_path='catalog/item.png',
        source_bucket='source-bucket',
        model_number=None,
        content_hash=SOURCE_HASH,
        source_size=len(SOURCE_BYTES),
        source_mime_type='image/png',
        source_width=40,
        source_height=24,
        normalization_version='preview-v1',
        oss_path='image-search/source-bucket/catalog/item.png',
        preview_oss_path=(
            f'image-search/previews/preview-v1/{SOURCE_HASH[:2]}/'
            f'{SOURCE_HASH}.jpg'
        ),
        preview_path=None,
        vector_values=[0.1] * EMBEDDING_DIMENSION,
        stages={},
    )


@pytest.mark.parametrize(
    ('winner_status', 'expected_status'),
    [('active', 'existing'), ('archived', 'in_recycle_bin')],
)
def test_unique_identity_race_converges_to_the_committed_winner(
    monkeypatch,
    winner_status,
    expected_status,
):
    winner = _existing_asset(status=winner_status)
    RaceImageAssetModel.query = IdentityQuery(identity_asset=winner)
    session = UniqueRaceSession()
    monkeypatch.setattr(ingest_module, 'ImageAsset', RaceImageAssetModel)
    monkeypatch.setattr(
        ingest_module,
        'db',
        SimpleNamespace(session=session),
    )
    service = ImageAssetIngestService(
        source=FakeSource(),
        storage=ExistingObjectStorage(winner),
        embedding_client=ForbiddenEmbedding(),
        normalizer=ForbiddenNormalizer(),
    )

    result = service._persist(
        _prepared_asset(),
        [0.1] * EMBEDDING_DIMENSION,
        commit=False,
    )

    assert result.status == expected_status
    assert result.asset_id == 'asset-stable-18'
    assert session.flush_calls == 1
    assert session.commits == 0
    assert session.rollbacks == 0


def test_unique_identity_race_with_different_content_stays_conflict(monkeypatch):
    winner = _existing_asset(status='active', content_hash='f' * 64)
    RaceImageAssetModel.query = IdentityQuery(identity_asset=winner)
    session = UniqueRaceSession()
    monkeypatch.setattr(ingest_module, 'ImageAsset', RaceImageAssetModel)
    monkeypatch.setattr(
        ingest_module,
        'db',
        SimpleNamespace(session=session),
    )
    service = ImageAssetIngestService(
        source=FakeSource(),
        storage=ExistingObjectStorage(winner),
        embedding_client=ForbiddenEmbedding(),
        normalizer=ForbiddenNormalizer(),
    )

    with pytest.raises(AssetIngestConflictError) as captured:
        service._persist(
            _prepared_asset(),
            [0.1] * EMBEDDING_DIMENSION,
            commit=False,
        )

    assert captured.value.kind == 'source_conflict'
    assert session.rollbacks == 0


def test_batch_failure_result_preserves_safe_source_conflict_identity():
    conflict = AssetIngestConflictError(
        '来源冲突',
        kind='source_conflict',
        asset_id='asset-existing-18',
        source_relative_path='catalog/item.png',
    )

    result = ImageAssetIngestService._failure_result(
        'fallback/path.png',
        conflict,
    )

    assert result.status == 'source_conflict'
    assert result.asset_id == 'asset-existing-18'
    assert result.source_relative_path == 'catalog/item.png'
    assert result.recovery_action is None
