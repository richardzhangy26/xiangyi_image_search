from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from services.purge_object_backup import VerifiedPurgeObjectBackup
from services.purge_object_restore import ObjectCopyVerification
from test_support.purge_manifest import complete_manifest


def _verified_evidence(manifest, digest="1" * 64):
    return VerifiedPurgeObjectBackup(
        status="complete",
        manifest_key=manifest.manifest_key,
        manifest_sha256=digest,
        manifest=manifest,
    )


def _batch_item(bundle):
    authorization = bundle.items[0]
    batch = SimpleNamespace(
        id=bundle.purge_batch_id,
        database_backup_id=bundle.database_backup_id,
        database_manifest_sha256=bundle.database_manifest_sha256,
        object_manifest_sha256=bundle.manifest_sha256,
        retain_until=bundle.retain_until.replace(tzinfo=None),
    )
    item = SimpleNamespace(
        target_asset_id=authorization.target_asset_id,
        formal_bucket=authorization.formal_bucket,
        original_formal_key=authorization.original_formal_key,
        original_backup_object_id=authorization.original_backup_object_id,
        original_backup_sha256=authorization.original_backup_sha256,
        preview_formal_key=authorization.preview_formal_key,
        preview_backup_object_id=authorization.preview_backup_object_id,
        preview_backup_sha256=authorization.preview_backup_sha256,
        preview_delete_authorized=authorization.preview_delete_authorized,
        authorization_retain_until=(
            authorization.authorization_retain_until.replace(tzinfo=None)
        ),
    )
    return batch, item


def _restore_point(manifest, **overrides):
    restore = manifest.database_restore_point
    retain_until = datetime.fromisoformat(
        str(restore["retain_until"]).replace("Z", "+00:00")
    )
    payload = dict(
        kind="purge_restore_point",
        backup_id=restore["backup_id"],
        artifact_sha256=restore["artifact_sha256"],
        retain_until=retain_until,
        purge_batch_id=restore["purge_batch_id"],
        manifest_sha256=restore["manifest_sha256"],
    )
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _verifier(manifest, bundle, now, *, restore_point=None, restore_point_loader=None):
    from services.formal_purge import CanonicalFormalPurgeAuthorizationVerifier

    if restore_point_loader is None:
        evidence = _restore_point(manifest) if restore_point is None else restore_point
        restore_point_loader = lambda _batch: evidence
    return CanonicalFormalPurgeAuthorizationVerifier(
        manifest_loader=lambda _batch: _verified_evidence(manifest),
        verify_copies=lambda parsed: ObjectCopyVerification(
            status="verified",
            manifest_sha256=bundle.manifest_sha256,
            object_count=len(parsed.objects),
        ),
        restore_point_loader=restore_point_loader,
        clock=lambda: now,
    )


def test_typed_canonical_evidence_allows_exact_item_snapshot():
    from services.formal_purge import CanonicalFormalPurgeAuthorizationVerifier
    from services.purge_formal_authorization import (
        build_formal_purge_authorization_bundle,
    )

    now = datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)
    manifest = complete_manifest()
    bundle = build_formal_purge_authorization_bundle(
        manifest, manifest_sha256="1" * 64, now=now,
    )
    batch, item = _batch_item(bundle)
    verifier = CanonicalFormalPurgeAuthorizationVerifier(
        manifest_loader=lambda _batch: _verified_evidence(manifest),
        verify_copies=lambda parsed: ObjectCopyVerification(
            status="verified",
            manifest_sha256=bundle.manifest_sha256,
            object_count=len(parsed.objects),
        ),
        restore_point_loader=lambda _batch: _restore_point(manifest),
        clock=lambda: now,
    )

    assert verifier.verify_for_operation(batch, item, "claim") is True
    assert verifier.verify_for_operation(batch, item, "original") is True
    assert verifier.verify_for_operation(batch, item, "preview") is True


def test_verifier_fails_closed_when_digest_copy_or_item_snapshot_differs():
    from services.formal_purge import CanonicalFormalPurgeAuthorizationVerifier
    from services.purge_formal_authorization import (
        build_formal_purge_authorization_bundle,
    )

    now = datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)
    manifest = complete_manifest()
    bundle = build_formal_purge_authorization_bundle(
        manifest, manifest_sha256="1" * 64, now=now,
    )
    batch, item = _batch_item(bundle)
    wrong_digest = CanonicalFormalPurgeAuthorizationVerifier(
        manifest_loader=lambda _batch: _verified_evidence(manifest, "2" * 64),
        verify_copies=lambda _manifest: True,
        restore_point_loader=lambda _batch: _restore_point(manifest),
        clock=lambda: now,
    )
    assert wrong_digest.verify_for_operation(batch, item, "claim") is False

    bad_copy = CanonicalFormalPurgeAuthorizationVerifier(
        manifest_loader=lambda _batch: _verified_evidence(manifest),
        verify_copies=lambda _manifest: False,
        restore_point_loader=lambda _batch: _restore_point(manifest),
        clock=lambda: now,
    )
    assert bad_copy.verify_for_operation(batch, item, "claim") is False

    item.original_backup_sha256 = "9" * 64
    exact = CanonicalFormalPurgeAuthorizationVerifier(
        manifest_loader=lambda _batch: _verified_evidence(manifest),
        verify_copies=lambda _manifest: True,
        restore_point_loader=lambda _batch: _restore_point(manifest),
        clock=lambda: now,
    )
    assert exact.verify_for_operation(batch, item, "claim") is False


def test_verifier_fails_closed_unless_live_restore_point_matches_batch_and_object_manifest():
    from services.purge_formal_authorization import (
        build_formal_purge_authorization_bundle,
    )

    now = datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)
    manifest = complete_manifest()
    bundle = build_formal_purge_authorization_bundle(
        manifest, manifest_sha256="1" * 64, now=now,
    )
    batch, item = _batch_item(bundle)
    restore = _restore_point(manifest)

    matching = _verifier(manifest, bundle, now, restore_point=restore)
    assert matching.verify_for_operation(batch, item, "claim") is True

    missing = _verifier(
        manifest, bundle, now, restore_point_loader=lambda _batch: None,
    )
    assert missing.verify_for_operation(batch, item, "claim") is False

    expired = _verifier(
        manifest,
        bundle,
        now,
        restore_point=_restore_point(
            manifest, retain_until=now - timedelta(seconds=1),
        ),
    )
    assert expired.verify_for_operation(batch, item, "claim") is False

    wrong_kind = _verifier(
        manifest,
        bundle,
        now,
        restore_point=_restore_point(manifest, kind="daily"),
    )
    assert wrong_kind.verify_for_operation(batch, item, "claim") is False

    wrong_sha = _verifier(
        manifest,
        bundle,
        now,
        restore_point=_restore_point(manifest, artifact_sha256="9" * 64),
    )
    assert wrong_sha.verify_for_operation(batch, item, "claim") is False

    wrong_backup_id = _verifier(
        manifest,
        bundle,
        now,
        restore_point=_restore_point(manifest, backup_id="purge-other-batch"),
    )
    assert wrong_backup_id.verify_for_operation(batch, item, "claim") is False
