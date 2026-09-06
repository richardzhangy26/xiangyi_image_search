"""Issue #28 additive grant-consumption and delete-permit schema."""

from sqlalchemy import text


MIGRATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS formal_deletion_grant_consumptions (
        grant_id VARCHAR(128) PRIMARY KEY,
        batch_id UUID NOT NULL UNIQUE,
        environment_id VARCHAR(128) NOT NULL,
        deployment_sha256 VARCHAR(64) NOT NULL,
        database_manifest_sha256 VARCHAR(64) NOT NULL,
        object_manifest_sha256 VARCHAR(64) NOT NULL,
        formal_bucket VARCHAR(255) NOT NULL,
        asset_scope_sha256 VARCHAR(64) NOT NULL,
        max_assets SMALLINT NOT NULL,
        max_object_deletes SMALLINT NOT NULL,
        used_object_deletes SMALLINT NOT NULL DEFAULT 0,
        issued_at TIMESTAMP NOT NULL,
        expires_at TIMESTAMP NOT NULL,
        consumed_at TIMESTAMP NOT NULL,
        state VARCHAR(16) NOT NULL DEFAULT 'active',
        trust_attestation_sha256 VARCHAR(64) NOT NULL,
        audit_retain_until TIMESTAMP NOT NULL,
        CONSTRAINT ck_formal_deletion_grant_state CHECK (state IN ('active', 'closed', 'expired')),
        CONSTRAINT ck_formal_deletion_grant_max_assets CHECK (max_assets BETWEEN 1 AND 20),
        CONSTRAINT ck_formal_deletion_grant_max_deletes CHECK (max_object_deletes BETWEEN 1 AND 40),
        CONSTRAINT ck_formal_deletion_grant_used_deletes CHECK (used_object_deletes >= 0 AND used_object_deletes <= max_object_deletes),
        CONSTRAINT ck_formal_deletion_grant_time_order CHECK (expires_at > issued_at)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_formal_deletion_grant_batch
    ON formal_deletion_grant_consumptions (batch_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_formal_deletion_grant_expiry
    ON formal_deletion_grant_consumptions (state, expires_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS formal_delete_call_permits (
        id UUID PRIMARY KEY,
        grant_id VARCHAR(128) NOT NULL REFERENCES formal_deletion_grant_consumptions(grant_id),
        batch_id UUID NOT NULL,
        target_asset_id UUID NOT NULL,
        operation_kind VARCHAR(16) NOT NULL,
        claim_generation BIGINT NOT NULL,
        formal_bucket VARCHAR(255) NOT NULL,
        formal_key TEXT NOT NULL,
        object_size BIGINT NOT NULL,
        object_sha256 VARCHAR(64) NOT NULL,
        object_etag TEXT NOT NULL,
        original_fence_id UUID NOT NULL,
        preview_fence_id UUID NOT NULL,
        state VARCHAR(16) NOT NULL DEFAULT 'issued',
        issued_at TIMESTAMP NOT NULL,
        executing_at TIMESTAMP,
        completed_at TIMESTAMP,
        cancelled_at TIMESTAMP,
        expires_at TIMESTAMP NOT NULL,
        result_code VARCHAR(80),
        audit_retain_until TIMESTAMP NOT NULL,
        CONSTRAINT uq_formal_delete_permit_item_operation UNIQUE (batch_id, target_asset_id, operation_kind),
        CONSTRAINT ck_formal_delete_permit_operation CHECK (operation_kind IN ('original', 'preview')),
        CONSTRAINT ck_formal_delete_permit_state CHECK (state IN ('issued', 'executing', 'completed', 'cancelled')),
        CONSTRAINT ck_formal_delete_permit_object_size CHECK (object_size > 0),
        CONSTRAINT ck_formal_delete_permit_time_order CHECK (expires_at > issued_at),
        CONSTRAINT ck_formal_delete_permit_state_times CHECK (
            (state = 'issued' AND executing_at IS NULL AND completed_at IS NULL AND cancelled_at IS NULL) OR
            (state = 'executing' AND executing_at IS NOT NULL AND completed_at IS NULL AND cancelled_at IS NULL) OR
            (state = 'completed' AND executing_at IS NOT NULL AND completed_at IS NOT NULL AND cancelled_at IS NULL) OR
            (state = 'cancelled' AND executing_at IS NULL AND completed_at IS NULL AND cancelled_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_formal_delete_permit_grant_state
    ON formal_delete_call_permits (grant_id, state)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_formal_delete_permit_batch_item
    ON formal_delete_call_permits (batch_id, target_asset_id, operation_kind)
    """,
)


def apply_migration(connection):
    for statement in MIGRATION_STATEMENTS:
        connection.execute(text(statement))
