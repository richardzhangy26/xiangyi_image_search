import uuid
from datetime import datetime

import pytest

from models import ImageAsset, ImageImportItem, db
from services.postgres_reference_snapshot import PostgresReferenceSnapshotReader


pytestmark = pytest.mark.postgresql


def test_postgres_snapshot_uses_isolated_schema_and_excludes_purged_imports(app):
    nonce = uuid.uuid4().hex
    asset = ImageAsset(
        source_provider='test', source_bucket='bucket', source_relative_path=f'{nonce}.png',
        source_revision=1, display_name='snapshot.png', oss_path=f'original/{nonce}',
        preview_oss_path=f'preview/{nonce}', content_hash='c' * 64, source_size=1,
        source_mime_type='image/png', source_width=1, source_height=1, vector=[0.0] * 1024,
        embedding_model='tongyi-embedding-vision-plus-2026-03-06', embedding_dimension=1024,
        normalization_version='preview-v1', status='archived',
    )
    purged = ImageImportItem(
        source_provider='local-import', source_bucket='bucket', source_relative_path=f'imports/{nonce}.png',
        source_revision=1, display_name='gone.png', oss_path=f'original/import-{nonce}',
        preview_oss_path=f'preview/import-{nonce}', content_hash='d' * 64, source_size=1,
        source_mime_type='image/png', source_width=1, source_height=1, normalization_version='preview-v1',
        request_id='issue-26-snapshot', status='abandoned', objects_purged_at=datetime.now(),
    )
    db.session.add_all([asset, purged])
    db.session.flush()
    asset_id, import_id = str(asset.id), str(purged.id)
    db.session.commit()

    snapshot = PostgresReferenceSnapshotReader(db.session).capture_for_purge((asset_id,))

    assert snapshot.targets[0].asset_id == asset_id
    assert all(reference.owner_id != import_id for reference in snapshot.references)
    assert {item.source for item in snapshot.source_slices} == {'image_assets', 'image_import_items'}
