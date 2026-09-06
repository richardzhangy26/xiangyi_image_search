"""Issue #27 的正式清除持久状态合同。"""

from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (BACKEND_DIR / relative_path).read_text(encoding='utf-8')


def test_formal_purge_model_declares_monotonic_delete_checkpoints():
    source = _read('models/purge_batch.py')

    assert "'deleting'" in source
    assert "'partial_failure'" in source
    for checkpoint in (
        'fenced',
        'original_delete_started',
        'original_deleted',
        'preview_delete_started',
        'preview_deleted',
        'preview_shared',
        'completed',
    ):
        assert repr(checkpoint) in source


def test_t13_worker_composition_cannot_name_delete_credentials_or_adapter():
    entry = _read('scripts/run_purge_batch_worker.py')

    assert 'PURGE_DELETE_OSS_' not in entry
    assert 'OssFormalObjectDeleter' not in entry


def test_formal_purge_migration_preserves_released_fence_epochs():
    migration = _read('migrations/issue_27_formal_purge.py')

    assert 'CREATE TABLE IF NOT EXISTS purge_object_fences' in migration
    assert 'released_at TIMESTAMP' in migration
    assert 'audit_retain_until TIMESTAMP NOT NULL' in migration
    assert 'CREATE UNIQUE INDEX IF NOT EXISTS uq_purge_object_fences_held_identity' in migration
    assert "WHERE state = 'held'" in migration
    assert 'CREATE TABLE IF NOT EXISTS purge_item_events' in migration


def test_fence_epoch_orm_keeps_audit_history_and_item_claim_fields():
    from models import PurgeBatchItem, PurgeObjectFence, PurgeItemEvent

    assert set(PurgeObjectFence.__table__.primary_key.columns.keys()) == {'id'}
    assert {'formal_bucket', 'formal_key', 'released_at', 'audit_retain_until'} <= set(
        PurgeObjectFence.__table__.columns.keys()
    )
    assert {'claim_token', 'claim_generation', 'lease_expires_at', 'checkpoint'} <= set(
        PurgeBatchItem.__table__.columns.keys()
    )
    assert {'batch_id', 'target_asset_id', 'event_type', 'audit_retain_until'} <= set(
        PurgeItemEvent.__table__.columns.keys()
    )
    held_identity = next(
        index for index in PurgeObjectFence.__table__.indexes
        if index.name == 'uq_purge_object_fences_held_identity'
    )
    assert str(held_identity.dialect_options['postgresql']['where']) == "state = 'held'"


def test_migration_and_bootstrap_schema_include_formal_item_leases_and_retention():
    migration = ' '.join(_read('migrations/issue_27_formal_purge.py').split())
    bootstrap = ' '.join(
        (BACKEND_DIR.parent / 'postgres/init/01_init.sql').read_text(
            encoding='utf-8'
        ).split()
    )

    for column in (
        'deleting_at TIMESTAMP',
        'partial_failure_at TIMESTAMP',
        "checkpoint VARCHAR(40) NOT NULL DEFAULT 'pending'",
        'claim_token UUID',
        'claim_generation BIGINT NOT NULL DEFAULT 0',
        'audit_retain_until TIMESTAMP',
    ):
        assert column in migration
        assert column in bootstrap
    assert "'deleting', 'partial_failure', 'completed'" in migration
    assert "'deleting', 'partial_failure', 'completed'" in bootstrap


def test_binding_fence_schema_has_leased_owner_contract():
    from models import ObjectBindingFence

    columns = set(ObjectBindingFence.__table__.columns.keys())
    assert {
        'formal_bucket', 'formal_key', 'owner_kind', 'owner_token', 'state',
        'lease_expires_at', 'released_at', 'release_reason',
    } <= columns
    source = _read('migrations/issue_27_formal_purge.py')
    assert 'CREATE TABLE IF NOT EXISTS object_binding_fences' in source
    assert 'uq_object_binding_fences_held_identity' in source
    assert "owner_kind IN ('asset_ingest', 'import_promotion', 'import_cleanup')" in source
    assert 'lease_expires_at > acquired_at' in source
    assert 'owner_generation' in columns


def test_item_schema_requires_persisted_formal_delete_authorization_snapshot():
    from models import PurgeBatchItem

    columns = set(PurgeBatchItem.__table__.columns.keys())
    assert {
        'original_formal_key', 'original_backup_object_id', 'original_backup_sha256',
        'preview_formal_key', 'preview_backup_object_id', 'preview_backup_sha256',
        'preview_delete_authorized', 'authorization_retain_until', 'formal_bucket',
    } <= columns


def test_public_purge_dto_exposes_only_safe_progress_summary():
    source = _read('models/purge_batch.py')
    for field in ('completed_count', 'failed_count', 'pending_count', 'cancellable', 'next_action'):
        assert repr(field) in source
    public_scope = source.split('class PurgeBatchItem', 1)[0].split('def to_public_dict', 1)[1]
    assert 'original_formal_key' not in public_scope
    assert 'preview_formal_key' not in public_scope
    assert 'backup_object_id' not in public_scope


def test_issue_28_grant_and_delete_permit_schema_are_additive_and_audited():
    from models import FormalDeleteCallPermit, FormalDeletionGrantConsumption

    grant_columns = set(FormalDeletionGrantConsumption.__table__.columns.keys())
    assert {
        'grant_id', 'batch_id', 'environment_id', 'deployment_sha256',
        'database_manifest_sha256', 'object_manifest_sha256', 'formal_bucket',
        'asset_scope_sha256', 'max_assets', 'max_object_deletes',
        'used_object_deletes', 'issued_at', 'expires_at', 'consumed_at',
        'state', 'trust_attestation_sha256', 'audit_retain_until',
    } <= grant_columns
    permit_columns = set(FormalDeleteCallPermit.__table__.columns.keys())
    assert {
        'id', 'grant_id', 'batch_id', 'target_asset_id', 'operation_kind',
        'claim_generation', 'formal_bucket', 'formal_key', 'object_size',
        'object_sha256', 'object_etag', 'original_fence_id',
        'preview_fence_id', 'state', 'issued_at', 'executing_at',
        'completed_at', 'cancelled_at', 'expires_at', 'result_code',
        'audit_retain_until',
    } <= permit_columns

    migration = ' '.join(
        _read('migrations/issue_28_formal_delete_permits.py').split()
    )
    bootstrap = ' '.join(
        (BACKEND_DIR.parent / 'postgres/init/01_init.sql').read_text(
            encoding='utf-8'
        ).split()
    )
    for contract in (
        'CREATE TABLE IF NOT EXISTS formal_deletion_grant_consumptions',
        'CREATE TABLE IF NOT EXISTS formal_delete_call_permits',
        'CONSTRAINT uq_formal_delete_permit_item_operation UNIQUE (batch_id, target_asset_id, operation_kind)',
        "state IN ('active', 'closed', 'expired')",
        "state IN ('issued', 'executing', 'completed', 'cancelled')",
        'used_object_deletes <= max_object_deletes',
        'audit_retain_until TIMESTAMP NOT NULL',
    ):
        assert contract in migration
        assert contract in bootstrap
