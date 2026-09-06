"""Issue #27 的显式、幂等、只扩展正式清除 schema 迁移。"""

from sqlalchemy import text


MIGRATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS object_binding_fences (
        id UUID PRIMARY KEY,
        formal_bucket VARCHAR(255) NOT NULL,
        formal_key TEXT NOT NULL,
        owner_kind VARCHAR(32) NOT NULL,
        owner_token UUID NOT NULL,
        owner_generation BIGINT NOT NULL DEFAULT 0,
        state VARCHAR(16) NOT NULL DEFAULT 'held',
        acquired_at TIMESTAMP NOT NULL,
        lease_expires_at TIMESTAMP NOT NULL,
        released_at TIMESTAMP,
        release_reason VARCHAR(32),
        CONSTRAINT ck_object_binding_fences_owner_kind CHECK (owner_kind IN ('asset_ingest', 'import_promotion', 'import_cleanup')),
        CONSTRAINT ck_object_binding_fences_state CHECK (state IN ('held', 'released')),
        CONSTRAINT ck_object_binding_fences_lease_order CHECK (lease_expires_at > acquired_at),
        CONSTRAINT ck_object_binding_fences_release_state CHECK ((state = 'held' AND released_at IS NULL AND release_reason IS NULL) OR (state = 'released' AND released_at IS NOT NULL AND release_reason IS NOT NULL))
    )
    """,
    """
    ALTER TABLE object_binding_fences
        ADD COLUMN IF NOT EXISTS owner_generation BIGINT NOT NULL DEFAULT 0
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_object_binding_fences_held_identity
    ON object_binding_fences (formal_bucket, formal_key) WHERE state = 'held'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_object_binding_fences_owner_expiry
    ON object_binding_fences (owner_token, state, lease_expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_object_binding_fences_identity_expiry
    ON object_binding_fences (formal_bucket, formal_key, state, lease_expires_at)
    """,
    """
    ALTER TABLE purge_batches
        ADD COLUMN IF NOT EXISTS deleting_at TIMESTAMP,
        ADD COLUMN IF NOT EXISTS partial_failure_at TIMESTAMP
    """,
    """
    ALTER TABLE purge_batches DROP CONSTRAINT IF EXISTS ck_purge_batches_status
    """,
    """
    ALTER TABLE purge_batches ADD CONSTRAINT ck_purge_batches_status CHECK (
        status IN ('queued', 'database_backup', 'object_backup', 'verifying',
                   'pending_deletion', 'deleting', 'partial_failure', 'completed',
                   'failed', 'cancelled')
    )
    """,
    """
    ALTER TABLE purge_batch_items
        ADD COLUMN IF NOT EXISTS checkpoint VARCHAR(40) NOT NULL DEFAULT 'pending',
        ADD COLUMN IF NOT EXISTS claim_token UUID,
        ADD COLUMN IF NOT EXISTS claim_generation BIGINT NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMP,
        ADD COLUMN IF NOT EXISTS original_delete_started_at TIMESTAMP,
        ADD COLUMN IF NOT EXISTS original_deleted_at TIMESTAMP,
        ADD COLUMN IF NOT EXISTS preview_delete_started_at TIMESTAMP,
        ADD COLUMN IF NOT EXISTS preview_deleted_at TIMESTAMP,
        ADD COLUMN IF NOT EXISTS preview_disposition VARCHAR(40),
        ADD COLUMN IF NOT EXISTS database_deleted_at TIMESTAMP,
        ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP,
        ADD COLUMN IF NOT EXISTS failed_at TIMESTAMP,
        ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS audit_retain_until TIMESTAMP,
        ADD COLUMN IF NOT EXISTS original_formal_key TEXT,
        ADD COLUMN IF NOT EXISTS original_backup_object_id VARCHAR(128),
        ADD COLUMN IF NOT EXISTS original_backup_sha256 VARCHAR(64),
        ADD COLUMN IF NOT EXISTS preview_formal_key TEXT,
        ADD COLUMN IF NOT EXISTS preview_backup_object_id VARCHAR(128),
        ADD COLUMN IF NOT EXISTS preview_backup_sha256 VARCHAR(64),
        ADD COLUMN IF NOT EXISTS preview_delete_authorized BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS authorization_retain_until TIMESTAMP
        , ADD COLUMN IF NOT EXISTS formal_bucket VARCHAR(255)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_purge_batch_items_claim
    ON purge_batch_items (status, lease_expires_at, batch_id, ordinal)
    """,
    """
    CREATE TABLE IF NOT EXISTS purge_object_fences (
        id UUID PRIMARY KEY,
        formal_bucket VARCHAR(255) NOT NULL,
        formal_key TEXT NOT NULL,
        kind VARCHAR(24) NOT NULL,
        batch_id UUID NOT NULL,
        target_asset_id UUID NOT NULL,
        state VARCHAR(24) NOT NULL DEFAULT 'held',
        acquired_at TIMESTAMP NOT NULL,
        released_at TIMESTAMP,
        audit_retain_until TIMESTAMP NOT NULL,
        CONSTRAINT ck_purge_object_fences_state CHECK (state IN ('held', 'released'))
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_purge_object_fences_held_identity
    ON purge_object_fences (formal_bucket, formal_key)
    WHERE state = 'held'
    """,
    """
    CREATE TABLE IF NOT EXISTS purge_item_events (
        id UUID PRIMARY KEY,
        batch_id UUID NOT NULL,
        target_asset_id UUID NOT NULL,
        event_type VARCHAR(80) NOT NULL,
        result_code VARCHAR(80),
        error_code VARCHAR(80),
        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
        audit_retain_until TIMESTAMP NOT NULL
    )
    """,
)


def apply_migration(connection):
    """由显式调用者提供事务；应用启动不会调用。"""
    for statement in MIGRATION_STATEMENTS:
        connection.execute(text(statement))
