"""#27 formal-purge 多会话覆盖：绑定/删除互斥、过期接管零调用、finalization 回滚、删除后向量永久缺席。

真实 PostgreSQL（concurrent_app 临时 schema）+ 伪 deleter；所有仓库操作经
db.session 或独立 Session 连接交叉并发，不触碰真实 OSS。
"""

from __future__ import annotations

import uuid
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from models import ImageAsset, PurgeBatch, PurgeBatchItem, PurgeObjectFence, db
from services.formal_purge import FormalPurgeRepository, FormalPurgeWorker
from services.object_binding_fence import ObjectBindingFenceService
from services.purge_object_fence import ObjectIdentity
from services.purge_object_storage import DeletionObservation, FormalObjectObservation
from services.vector_search import VectorSearchService
from test_support.formal_grant import StaticFormalCapability, formal_grant_for


def _asset(color='red'):
    nonce = uuid.uuid4().hex
    return ImageAsset(
        source_provider='test', source_bucket='source',
        source_relative_path=f'purge/{nonce}.png', source_revision=1,
        display_name='purge.png', oss_path=f'original/{nonce}',
        preview_oss_path=f'preview/{nonce}', content_hash=nonce,
        source_size=1, source_mime_type='image/png', source_width=1,
        source_height=1, vector=[0.1] * 1024,
        embedding_model='tongyi-embedding-vision-plus-2026-03-06',
        embedding_dimension=1024, normalization_version='preview-v1',
        status='archived',
    )


def _batch_item(asset):
    batch = PurgeBatch(
        actor_id='admin', idempotency_key=f'key.{uuid.uuid4().hex}',
        request_fingerprint_sha256='a' * 64, confirmation_text='永久删除 1 张',
        status='pending_deletion', retain_until=datetime.now() + timedelta(days=1),
        database_backup_id='purge-test', database_manifest_sha256='d' * 64,
        object_manifest_sha256='e' * 64,
    )
    item = PurgeBatchItem(
        batch=batch, target_asset_id=asset.id, ordinal=0,
        original_formal_key=asset.oss_path, original_backup_object_id='orig-copy',
        original_backup_sha256='b' * 64, preview_formal_key=asset.preview_oss_path,
        preview_backup_object_id='preview-copy', preview_backup_sha256='c' * 64,
        preview_delete_authorized=True,
        authorization_retain_until=datetime.now() + timedelta(days=1),
        formal_bucket='formal-test-bucket',
    )
    return batch, item


def _seed(assets, batch, item):
    db.session.add_all(assets)
    db.session.flush()  # asset.id 由客户端默认生成于 flush，之后再绑定墓碑主键
    item.target_asset_id = assets[0].id
    db.session.add_all([batch, item])
    db.session.commit()


@contextmanager
def _sessions(app):
    with app.app_context():
        created = []
        schema_name = db.session.execute(text('SELECT current_schema()')).scalar_one()
        assert re.fullmatch(r'[a-z0-9_]+', schema_name)

        def Session():
            session = sessionmaker(bind=db.engine)()
            session.execute(text(f'SET search_path TO "{schema_name}", public'))
            session.commit()
            created.append(session)
            return session

        try:
            yield Session
        finally:
            for session in created:
                session.close()
            db.session.remove()


class RecordingDeleter:
    def __init__(self, item):
        self.item = item
        self.calls = []
        self.deleted = set()

    def observe(self, key):
        if key in self.deleted:
            return None
        operation = 'original' if key == self.item.original_formal_key else 'preview'
        return _observation(self.item, operation)

    def delete(self, authorization):
        self.calls.append(authorization.formal_key)
        self.deleted.add(authorization.formal_key)
        return DeletionObservation(
            result='deleted', before=authorization.observation,
            deleted_at=datetime.now(timezone.utc), after_missing=True,
        )


def _observation(item, operation_kind):
    return FormalObjectObservation(
        formal_bucket=item.formal_bucket,
        formal_key=(
            item.original_formal_key
            if operation_kind == 'original'
            else item.preview_formal_key
        ),
        size=1,
        sha256=(
            item.original_backup_sha256
            if operation_kind == 'original'
            else item.preview_backup_sha256
        ),
        etag=f'etag-{operation_kind}',
        observed_at=datetime.now(timezone.utc),
    )


def test_held_binding_lease_blocks_authorize_until_released(concurrent_app):
    with _sessions(concurrent_app) as Session:
        asset = _asset()
        batch, item = _batch_item(asset)
        _seed([asset], batch, item)
        repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
        claim = repo.claim_next_item()
        grant = formal_grant_for(batch, item)

        binding_session = Session()
        lease = ObjectBindingFenceService(binding_session).acquire(
            (
                ObjectIdentity('formal-test-bucket', item.original_formal_key),
                ObjectIdentity('formal-test-bucket', item.preview_formal_key),
            ),
            owner_kind='asset_ingest', lease_seconds=60,
        )
        try:
            # 活绑定租约必须阻止删除授权（authorize 在完整集合锁内复核绑定围栏）。
            assert repo.begin_delete_intent(
                claim,
                operation_kind='original',
                observation=_observation(item, 'original'),
                grant=grant,
            ) is None
            # 围栏仍归绑定方；没有部分 purge fence 被提交。
            observer = Session()
            assert observer.query(PurgeObjectFence).filter_by(
                batch_id=batch.id, state='held',
            ).count() == 0
        finally:
            releaser = Session()
            assert ObjectBindingFenceService(releaser).release(
                lease, reason='completed',
            ) is True
            releaser.close()

        authorized = repo.begin_delete_intent(
            claim,
            operation_kind='original',
            observation=_observation(item, 'original'),
            grant=grant,
        )
        assert authorized is not None and len(authorized.fence_ids) == 2


def test_expired_claim_takeover_on_second_session_zero_stale_deleter_calls(concurrent_app):
    with _sessions(concurrent_app) as Session:
        asset = _asset()
        batch, item = _batch_item(asset)
        _seed([asset], batch, item)
        original_key, preview_key = asset.oss_path, asset.preview_oss_path
        asset_id = asset.id
        repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
        stale_claim = repo.claim_next_item()
        assert stale_claim is not None
        db.session.execute(text(
            "UPDATE purge_batch_items SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE batch_id = :b"
        ), {'b': str(batch.id)})
        db.session.commit()

        takeover_session = Session()
        takeover_repo = FormalPurgeRepository(
            takeover_session, manifest_validator=lambda _b, _i: True,
        )
        grant = formal_grant_for(batch, item)
        # 旧 token 的 checkpoint/authorize 都被 CAS 拒绝，授权门先于任何外部删除。
        assert takeover_repo.checkpoint(stale_claim, 'original_delete_started') is False
        assert takeover_repo.begin_delete_intent(
            stale_claim,
            operation_kind='original',
            observation=_observation(item, 'original'),
            grant=grant,
        ) is None

        # 第二会话 worker 经 claim_next_item 接管过期 in_progress（generation+1、
        # 换 token）并正常完成；删除调用恰来自新租约的一次合法运行。
        fresh_deleter = RecordingDeleter(item)
        worker_session = Session()
        worker = FormalPurgeWorker(
            repository=FormalPurgeRepository(
                worker_session, manifest_validator=lambda _b, _i: True,
            ),
            capability=StaticFormalCapability(grant),
            capability_context=grant.context,
            deleter=fresh_deleter,
        )
        assert worker.process_one_item() is True
        assert sorted(fresh_deleter.calls) == sorted([original_key, preview_key])
        observer = Session()
        assert observer.query(ImageAsset).filter_by(id=asset_id).count() == 0
        # 接管后旧 claim 再尝试授权仍为 None（token/generation 已换代）。
        assert takeover_repo.begin_delete_intent(
            stale_claim,
            operation_kind='original',
            observation=_observation(item, 'original'),
            grant=grant,
        ) is None


def test_finalization_commit_failure_rolls_back_and_retry_finalizes(concurrent_app):
    class _OnceFailingCommit:
        """代理 db.session：finalize 的第一次 commit 抛错，模拟提交前崩溃。"""

        def __init__(self, inner):
            object.__setattr__(self, '_inner', inner)
            object.__setattr__(self, 'armed', False)

        def commit(self):
            if object.__getattribute__(self, 'armed'):
                object.__setattr__(self, 'armed', False)
                raise RuntimeError('simulated commit failure')
            return object.__getattribute__(self, '_inner').commit()

        def __getattr__(self, name):
            return getattr(object.__getattribute__(self, '_inner'), name)

    with _sessions(concurrent_app) as Session:
        asset = _asset()
        batch, item = _batch_item(asset)
        _seed([asset], batch, item)
        wrapped = _OnceFailingCommit(db.session)
        repo = FormalPurgeRepository(wrapped, manifest_validator=lambda _b, _i: True)
        claim = repo.claim_next_item()
        grant = formal_grant_for(batch, item)
        authorization = repo.begin_delete_intent(
            claim,
            operation_kind='original',
            observation=_observation(item, 'original'),
            grant=grant,
        )
        assert authorization is not None
        executing = repo.start_delete_call(authorization)
        assert executing is not None
        assert repo.complete_delete_call(
            executing,
            DeletionObservation(
                result='deleted', before=authorization.observation,
                deleted_at=datetime.now(timezone.utc), after_missing=True,
            ),
        ) is True
        assert repo.checkpoint(claim, 'preview_shared') is True

        armed_commit = wrapped
        object.__setattr__(armed_commit, 'armed', True)
        with pytest.raises(RuntimeError, match='simulated commit failure'):
            repo.finalize(claim)

        # 回滚后状态完全一致：资产行仍在、item 仍持有原 claim、两把删除围栏仍 held。
        observer = Session()
        assert observer.query(ImageAsset).filter_by(
            id=asset.id, status='archived',
        ).count() == 1
        assert observer.query(PurgeObjectFence).filter_by(
            batch_id=batch.id, state='held',
        ).count() == 2
        row = observer.get(PurgeBatchItem, (batch.id, asset.id))
        assert row.status == 'in_progress' and row.checkpoint == 'preview_shared'

        # 同一 claim 重试 finalize 成功并完成释放。
        assert repo.finalize(claim) is True
        observer.expire_all()
        assert observer.query(ImageAsset).filter_by(id=asset.id).count() == 0
        assert observer.query(PurgeObjectFence).filter_by(
            batch_id=batch.id, state='held',
        ).count() == 0


def test_deleted_asset_permanently_absent_from_vector_search(concurrent_app):
    with _sessions(concurrent_app) as Session:
        target = _asset()
        decoy = _asset()
        decoy.status = 'active'
        batch, item = _batch_item(target)
        _seed([target, decoy], batch, item)
        target_id, decoy_id = str(target.id), str(decoy.id)

        grant = formal_grant_for(batch, item)
        worker = FormalPurgeWorker(
            repository=FormalPurgeRepository(
                db.session, manifest_validator=lambda _b, _i: True,
            ),
            capability=StaticFormalCapability(grant),
            capability_context=grant.context,
            deleter=RecordingDeleter(item),
        )
        assert worker.process_one_item() is True
        db.session.remove()

        service = VectorSearchService()
        found_ids = [
            row['asset_id']
            for row in service.search_by_vector([0.1] * 1024, top_k=10)
        ]
        assert decoy_id in found_ids
        assert target_id not in found_ids
        observer = Session()
        assert observer.query(ImageAsset).filter_by(id=uuid.UUID(target_id)).count() == 0
        # 二次检索结果稳定：删除不可复活。
        assert target_id not in [
            row['asset_id']
            for row in service.search_by_vector([0.1] * 1024, top_k=10)
        ]
