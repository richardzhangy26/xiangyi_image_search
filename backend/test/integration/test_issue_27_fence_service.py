import uuid
from datetime import datetime, timedelta

import pytest

from models import ImageAsset, ImageImportItem, PurgeObjectFence, db


def _held_fence():
    now = datetime.now()
    return PurgeObjectFence(
        id=uuid.uuid4(),
        formal_bucket='private-formal-bucket',
        formal_key='preview/held.jpg',
        kind='search_preview',
        batch_id=uuid.uuid4(),
        target_asset_id=uuid.uuid4(),
        state='held',
        acquired_at=now,
        audit_retain_until=now + timedelta(days=365),
    )


def test_held_epoch_blocks_binding_but_released_epoch_does_not(app):
    from services.purge_object_fence import (
        ObjectBindingBlocked,
        ObjectIdentity,
        PurgeObjectFenceService,
    )

    fence = _held_fence()
    db.session.add(fence)
    db.session.commit()
    service = PurgeObjectFenceService(db.session)
    identity = ObjectIdentity('private-formal-bucket', 'preview/held.jpg')

    with pytest.raises(ObjectBindingBlocked):
        service.assert_bindable((identity,))
    db.session.rollback()

    fence.state = 'released'
    fence.released_at = datetime.now()
    db.session.commit()
    service.assert_bindable((identity,))


def test_fence_service_acquires_and_releases_a_new_epoch(app):
    from services.purge_object_fence import ObjectIdentity, PurgeObjectFenceService

    service = PurgeObjectFenceService(db.session)
    identity = ObjectIdentity('private-formal-bucket', 'original/one.png')
    batch_id, asset_id = uuid.uuid4(), uuid.uuid4()

    fence = service.acquire_for_deletion(
        batch_id=batch_id,
        target_asset_id=asset_id,
        identity=identity,
        kind='source_image',
        audit_retain_until=datetime.now() + timedelta(days=365),
    )
    db.session.commit()
    assert fence.state == 'held'

    service.release(fence.id, released_at=datetime.now())
    db.session.commit()
    assert db.session.get(PurgeObjectFence, fence.id).state == 'released'


def _asset(*, original, preview, status='archived'):
    nonce = uuid.uuid4().hex
    return ImageAsset(
        source_provider='test', source_bucket='source-bucket',
        source_relative_path=f'assets/{nonce}.png', source_revision=1,
        display_name='asset.png', oss_path=original, preview_oss_path=preview,
        content_hash=nonce, source_size=1, source_mime_type='image/png',
        source_width=1, source_height=1, vector=[0.0] * 1024,
        embedding_model='tongyi-embedding-vision-plus-2026-03-06',
        embedding_dimension=1024, normalization_version='preview-v1', status=status,
    )


def _import(*, preview, asset_id, status):
    nonce = uuid.uuid4().hex
    return ImageImportItem(
        source_provider='test', source_bucket='source-bucket',
        source_relative_path=f'imports/{nonce}.png', source_revision=1,
        display_name='import.png', oss_path=f'original/import-{nonce}',
        preview_oss_path=preview, content_hash=nonce, source_size=1,
        source_mime_type='image/png', source_width=1, source_height=1,
        normalization_version='preview-v1', request_id='issue-27',
        asset_id=asset_id, status=status,
    )


def test_current_reference_scan_ignores_completed_target_lineage_but_keeps_other_refs(app):
    from services.purge_object_fence import ObjectIdentity, PurgeObjectFenceService

    target = _asset(original='original/target', preview='preview/shared')
    db.session.add(target)
    db.session.flush()
    db.session.add_all([
        _import(preview='preview/shared', asset_id=target.id, status='completed'),
        _import(preview='preview/shared', asset_id=None, status='queued'),
        _asset(original='original/other', preview='preview/shared', status='active'),
    ])
    db.session.commit()

    decision = PurgeObjectFenceService(db.session).current_references(
        target_asset_id=target.id,
        identity=ObjectIdentity('private-formal-bucket', 'preview/shared'),
        kind='search_preview',
    )
    assert decision.asset_reference_count == 1
    assert decision.import_reference_count == 1
    assert decision.has_other_references is True
