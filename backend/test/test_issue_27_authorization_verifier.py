from datetime import datetime, timedelta


def test_verifier_fails_closed_when_manifest_digest_or_copy_verification_fails():
    from services.formal_purge import CanonicalFormalPurgeAuthorizationVerifier

    batch = type('Batch', (), {
        'id': 'batch-1', 'object_manifest_sha256': 'expected',
        'retain_until': datetime.now() + timedelta(days=1),
    })()
    item = type('Item', (), {
        'target_asset_id': 'asset-1', 'original_formal_key': 'original/a',
        'original_backup_object_id': 'orig-copy', 'original_backup_sha256': 'a' * 64,
        'preview_formal_key': 'preview/a', 'preview_backup_object_id': 'preview-copy',
        'preview_backup_sha256': 'b' * 64, 'preview_delete_authorized': True,
        'authorization_retain_until': datetime.now() + timedelta(days=1),
    })()
    verifier = CanonicalFormalPurgeAuthorizationVerifier(
        manifest_loader=lambda _batch: ({'sha256': 'wrong', 'batch_id': 'batch-1', 'items': []}),
        verify_copies=lambda _manifest: True,
    )
    assert verifier.verify_for_operation(batch, item, 'claim') is False
