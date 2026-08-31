"""Binding lease closes the import-cleanup object/write checkpoint."""

import uuid
from datetime import datetime, timedelta

from models import ImageImportItem, ObjectBindingFence, db
from services.import_cleanup import cleanup_one_item
from services.object_binding_fence import ObjectBindingFenceService
from services.purge_object_fence import PurgeObjectFenceService


class _FakeStorage:
    def __init__(self):
        self.deleted = []

    def delete_object(self, key):
        self.deleted.append(key)
        return 'already_gone'


def test_cleanup_final_bind_releases_lease_and_marks_checkpoint(app):
    nonce = uuid.uuid4().hex
    item = ImageImportItem(
        source_provider='test', source_bucket='source',
        source_relative_path=f'cleanup/{nonce}.png', source_revision=1,
        display_name='cleanup.png', oss_path=f'original/{nonce}',
        preview_oss_path=f'preview/{nonce}', content_hash=nonce,
        source_size=1, source_mime_type='image/png', source_width=1,
        source_height=1, normalization_version='preview-v1',
        request_id='issue-27-cleanup', status='cancelled',
        purge_eligible_at=datetime.now() - timedelta(seconds=1),
    )
    db.session.add(item)
    db.session.commit()

    storage = _FakeStorage()
    service = ObjectBindingFenceService(
        db.session, purge_fence_service=PurgeObjectFenceService(db.session),
    )
    assert cleanup_one_item(
        db.session, item.id, storage=storage,
        binding_fence_service=service, formal_bucket='formal-test-bucket',
    ) is True
    assert db.session.get(ImageImportItem, item.id).objects_purged_at is not None
    assert ObjectBindingFence.query.filter_by(state='held').count() == 0
