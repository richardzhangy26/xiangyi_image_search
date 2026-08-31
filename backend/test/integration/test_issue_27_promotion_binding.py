"""Promotion binds the completed asset while its complete binding lease is live."""

import uuid
from datetime import datetime

from models import ImageImportItem, ObjectBindingFence, db
from services.image_import_worker import _claim_snapshot, complete_import_item
from services.object_binding_fence import ObjectBindingFenceService
from services.purge_object_fence import PurgeObjectFenceService


def test_promotion_final_bind_creates_asset_and_releases_lease(app):
    nonce = uuid.uuid4().hex
    token = uuid.uuid4()
    item = ImageImportItem(
        source_provider='test', source_bucket='source',
        source_relative_path=f'promotion/{nonce}.png', source_revision=1,
        display_name='promotion.png', oss_path=f'original/{nonce}',
        preview_oss_path=f'preview/{nonce}', content_hash=nonce,
        source_size=1, source_mime_type='image/png', source_width=1,
        source_height=1, normalization_version='preview-v1',
        request_id='issue-27-promotion', status='embedding',
        claim_token=token, claimed_by='test', claim_generation=1,
        created_at=datetime.now(),
    )
    db.session.add(item)
    db.session.commit()
    claim = _claim_snapshot(db.session.get(ImageImportItem, item.id))

    service = ObjectBindingFenceService(
        db.session, purge_fence_service=PurgeObjectFenceService(db.session),
    )
    assert complete_import_item(
        db.session, claim, [0.1] * 1024,
        binding_fence_service=service, formal_bucket='formal-test-bucket',
    ) is True
    completed = db.session.get(ImageImportItem, item.id)
    assert completed.status == 'completed'
    assert completed.asset_id is not None
    assert ObjectBindingFence.query.filter_by(state='held').count() == 0
