import uuid
from datetime import datetime, timezone

from test_support.purge_manifest import complete_manifest


def test_complete_manifest_builds_one_typed_item_authorization():
    from services.purge_formal_authorization import (
        build_formal_purge_authorization_bundle,
    )

    manifest = complete_manifest()
    bundle = build_formal_purge_authorization_bundle(
        manifest,
        manifest_sha256="1" * 64,
        now=datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc),
    )

    assert bundle.purge_batch_id == uuid.UUID(manifest.purge_batch_id)
    assert bundle.manifest_sha256 == "1" * 64
    assert bundle.database_backup_id == manifest.database_restore_point["backup_id"]
    assert bundle.database_manifest_sha256 == manifest.database_restore_point["manifest_sha256"]
    assert bundle.retain_until == datetime.fromisoformat(manifest.retention["retain_until"])
    assert len(bundle.items) == 1
    authorization = bundle.items[0]
    assert authorization.target_asset_id == uuid.UUID(manifest.asset_ids[0])
    assert authorization.formal_bucket == "formal-test-bucket"
    assert authorization.original_formal_key == f"original/{manifest.asset_ids[0]}"
    assert authorization.original_backup_object_id == manifest.objects[0].object_id
    assert authorization.original_backup_sha256 == "a" * 64
    assert authorization.preview_formal_key == "preview/shared"
    assert authorization.preview_backup_object_id == manifest.objects[1].object_id
    assert authorization.preview_backup_sha256 == "b" * 64
    assert authorization.preview_delete_authorized is True
    assert authorization.authorization_retain_until == bundle.retain_until


def test_typed_bundle_atomically_promotes_all_item_authorization_fields():
    from app import create_app
    from models import ImageAsset, PurgeBatch, PurgeBatchItem, db
    from services.purge_batch_control import PurgeBatchControlService
    from services.purge_formal_authorization import (
        build_formal_purge_authorization_bundle,
    )

    app = create_app("testing")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    with app.app_context():
        db.create_all()
        manifest = complete_manifest()
        bundle = build_formal_purge_authorization_bundle(
            manifest,
            manifest_sha256="1" * 64,
            now=datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc),
        )
        authorization = bundle.items[0]
        asset = ImageAsset(
            id=authorization.target_asset_id,
            source_provider="test",
            source_bucket="source",
            source_relative_path="asset.png",
            source_revision=1,
            display_name="asset.png",
            oss_path=authorization.original_formal_key,
            preview_oss_path=authorization.preview_formal_key,
            content_hash="9" * 64,
            source_size=1,
            source_mime_type="image/png",
            source_width=1,
            source_height=1,
            vector=[0.0] * 1024,
            embedding_model="tongyi-embedding-vision-plus-2026-03-06",
            embedding_dimension=1024,
            normalization_version="preview-v1",
            status="archived",
        )
        batch = PurgeBatch(
            id=bundle.purge_batch_id,
            actor_id="admin",
            idempotency_key="issue28.b1.atomic",
            request_fingerprint_sha256="8" * 64,
            confirmation_text="永久删除 1 张",
            status="verifying",
            database_backup_id=bundle.database_backup_id,
            database_manifest_sha256=bundle.database_manifest_sha256,
            object_manifest_sha256=bundle.manifest_sha256,
            retain_until=bundle.retain_until.replace(tzinfo=None),
        )
        item = PurgeBatchItem(
            batch=batch,
            target_asset_id=asset.id,
            ordinal=0,
            status="pending",
        )
        db.session.add_all([asset, batch, item])
        db.session.commit()

        assert PurgeBatchControlService(
            db.session
        ).advance_verified_to_pending_if_current(bundle) is True

        promoted_batch = db.session.get(PurgeBatch, batch.id)
        promoted = db.session.get(PurgeBatchItem, (batch.id, asset.id))
        assert promoted_batch.status == "pending_deletion"
        assert promoted.status == "pending"
        assert promoted.checkpoint == "pending"
        assert promoted.formal_bucket == authorization.formal_bucket
        assert promoted.original_formal_key == authorization.original_formal_key
        assert promoted.original_backup_object_id == authorization.original_backup_object_id
        assert promoted.original_backup_sha256 == authorization.original_backup_sha256
        assert promoted.preview_formal_key == authorization.preview_formal_key
        assert promoted.preview_backup_object_id == authorization.preview_backup_object_id
        assert promoted.preview_backup_sha256 == authorization.preview_backup_sha256
        assert promoted.preview_delete_authorized is True
        assert promoted.authorization_retain_until == bundle.retain_until.replace(tzinfo=None)
        db.session.remove()
        db.drop_all()
