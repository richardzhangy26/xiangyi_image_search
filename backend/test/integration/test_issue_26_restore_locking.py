"""Issue #26 恢复锁的真实 PostgreSQL 合同；无库时 skip。"""

import uuid

import pytest

from models import ImageAsset, db
from services.asset_recycle_bin import RestoreBlockedByPurgeBatch, restore_image_assets
from services.purge_batch_control import PurgeBatchControlService

pytestmark = pytest.mark.postgresql


def test_active_purge_batch_blocks_restore_on_postgres(app):
    nonce = uuid.uuid4().hex
    asset = ImageAsset(
        source_provider='test', source_bucket='bucket',
        source_relative_path=f'{nonce}.png', source_revision=1,
        display_name='lock.png', oss_path=f'original/{nonce}',
        preview_oss_path=f'preview/{nonce}', content_hash='c' * 64,
        source_size=1, source_mime_type='image/png', source_width=1,
        source_height=1, vector=[0.0] * 1024,
        embedding_model='tongyi-embedding-vision-plus-2026-03-06',
        embedding_dimension=1024, normalization_version='preview-v1',
        status='archived',
    )
    db.session.add(asset)
    db.session.commit()
    PurgeBatchControlService(db.session).create_or_replay(
        actor_id='admin',
        idempotency_key='key.lock.1',
        asset_ids=[asset.id],
        confirmation='永久删除 1 张',
        request_id='create',
    )
    with pytest.raises(RestoreBlockedByPurgeBatch):
        restore_image_assets(db.session, [str(asset.id)], request_id='restore')
