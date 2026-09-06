"""Issue #26 worker claim/CAS 的真实 PostgreSQL 合同；无库时 skip。"""

import uuid

import pytest

from models import ImageAsset, PurgeBatch, db
from services.purge_batch_control import PurgeBatchControlService

pytestmark = pytest.mark.postgresql


def _asset():
    nonce = uuid.uuid4().hex
    return ImageAsset(
        source_provider='test',
        source_bucket='test-bucket',
        source_relative_path=f'assets/{nonce}.png',
        source_revision=1,
        display_name='asset.png',
        oss_path=f'original/{nonce}',
        preview_oss_path=f'preview/{nonce}',
        content_hash=nonce,
        source_size=1,
        source_mime_type='image/png',
        source_width=1,
        source_height=1,
        vector=[0.0] * 1024,
        embedding_model='tongyi-embedding-vision-plus-2026-03-06',
        embedding_dimension=1024,
        normalization_version='preview-v1',
        status='archived',
    )


def test_cancel_generation_wins_over_late_object_backup_result(app):
    asset = _asset()
    db.session.add(asset)
    db.session.commit()
    control = PurgeBatchControlService(db.session)
    created = control.create_or_replay(
        actor_id='admin',
        idempotency_key='key.late.01',
        asset_ids=[asset.id],
        confirmation='永久删除 1 张',
        request_id='create',
    ).batch
    claim = control.claim_next(worker_id='w1', lease_seconds=30)
    control.cancel(created.id, actor_id='admin', request_id='cancel')

    assert control.advance_if_current(claim, status='verifying') is False
    assert db.session.get(PurgeBatch, created.id).status == 'cancelled'
    assert control.claim_next(worker_id='w2', lease_seconds=30) is None
