import uuid

from app import create_app
from models import ImageAsset, ImageImportItem, db


def _app():
    app = create_app('testing')
    with app.app_context():
        db.create_all()
    return app


def _asset(*, status='archived', preview=None):
    nonce = uuid.uuid4().hex
    return ImageAsset(
        source_provider='test', source_bucket='bucket', source_relative_path=f'{nonce}.png',
        source_revision=1, display_name='image.png', oss_path=f'original/{nonce}',
        preview_oss_path=preview or f'preview/{nonce}', content_hash=nonce,
        source_size=1, source_mime_type='image/png', source_width=1, source_height=1,
        vector=[0.0] * 1024, embedding_model='tongyi-embedding-vision-plus-2026-03-06',
        embedding_dimension=1024, normalization_version='preview-v1', status=status,
    )


def test_snapshot_covers_assets_and_unpurged_import_preview_references():
    app = _app()
    with app.app_context():
        shared_preview = 'preview/shared.jpg'
        target, other = _asset(preview=shared_preview), _asset(status='active', preview=shared_preview)
        item = ImageImportItem(
            source_provider='local-import', source_bucket='bucket', source_relative_path='imports/one.png',
            source_revision=1, display_name='one.png', oss_path='original/import-one',
            preview_oss_path=shared_preview, content_hash='a' * 64, source_size=1,
            source_mime_type='image/png', source_width=1, source_height=1,
            normalization_version='preview-v1', request_id='snapshot-test', status='completed',
        )
        db.session.add_all([target, other, item])
        db.session.flush()
        target_id = str(target.id)
        db.session.commit()

        from services.postgres_reference_snapshot import PostgresReferenceSnapshotReader
        snapshot = PostgresReferenceSnapshotReader(db.session).capture_for_purge((target_id,))

        assert [entry.source for entry in snapshot.source_slices] == ['image_assets', 'image_import_items']
        assert {entry.owner_id for entry in snapshot.references if entry.formal_key == shared_preview} == {
            str(target.id), str(other.id), str(item.id)
        }
        assert snapshot.targets[0].asset_id == str(target.id)
        assert snapshot.targets[0].status == 'archived'


def test_snapshot_excludes_import_items_already_marked_objects_purged():
    app = _app()
    with app.app_context():
        target = _asset()
        item = ImageImportItem(
            source_provider='local-import', source_bucket='bucket', source_relative_path='imports/gone.png',
            source_revision=1, display_name='gone.png', oss_path='original/gone', preview_oss_path='preview/gone',
            content_hash='b' * 64, source_size=1, source_mime_type='image/png', source_width=1,
            source_height=1, normalization_version='preview-v1', request_id='snapshot-test',
            status='abandoned', objects_purged_at=__import__('datetime').datetime.now(),
        )
        db.session.add_all([target, item])
        db.session.flush()
        target_id = str(target.id)
        db.session.commit()
        from services.postgres_reference_snapshot import PostgresReferenceSnapshotReader
        snapshot = PostgresReferenceSnapshotReader(db.session).capture_for_purge((target_id,))
        assert all(reference.owner_id != str(item.id) for reference in snapshot.references)


def test_reader_declares_postgresql_read_only_repeatable_read_contract():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'services/postgres_reference_snapshot.py').read_text(encoding='utf-8')
    assert 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY' in source
