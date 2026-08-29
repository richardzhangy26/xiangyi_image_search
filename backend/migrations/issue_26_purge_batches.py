"""Issue #26 显式、幂等、只扩展的 PostgreSQL 清除批次迁移。"""

import argparse

from sqlalchemy import text


MIGRATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS purge_batches (
        id UUID PRIMARY KEY,
        actor_id VARCHAR(128) NOT NULL,
        idempotency_key VARCHAR(128) NOT NULL,
        request_fingerprint_sha256 VARCHAR(64) NOT NULL,
        confirmation_text VARCHAR(64) NOT NULL,
        status VARCHAR(24) NOT NULL DEFAULT 'queued',
        claim_token UUID,
        claim_generation BIGINT NOT NULL DEFAULT 0,
        claimed_by VARCHAR(128),
        lease_expires_at TIMESTAMP,
        database_backup_id VARCHAR(160),
        database_manifest_sha256 VARCHAR(64),
        object_manifest_sha256 VARCHAR(64),
        retain_until TIMESTAMP,
        error_code VARCHAR(80),
        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
        started_at TIMESTAMP,
        completed_at TIMESTAMP,
        failed_at TIMESTAMP,
        cancelled_at TIMESTAMP,
        CONSTRAINT uq_purge_batches_actor_key UNIQUE (actor_id, idempotency_key),
        CONSTRAINT ck_purge_batches_status CHECK (status IN ('queued', 'database_backup', 'object_backup', 'verifying', 'pending_deletion', 'failed', 'cancelled')),
        CONSTRAINT ck_purge_batches_claim_generation CHECK (claim_generation >= 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS purge_batch_items (
        batch_id UUID NOT NULL REFERENCES purge_batches(id) ON DELETE CASCADE,
        target_asset_id UUID NOT NULL,
        ordinal SMALLINT NOT NULL,
        status VARCHAR(24) NOT NULL DEFAULT 'queued',
        result_code VARCHAR(80),
        error_code VARCHAR(80),
        checkpoint_at TIMESTAMP,
        PRIMARY KEY (batch_id, target_asset_id),
        CONSTRAINT ck_purge_batch_items_ordinal CHECK (ordinal >= 0)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_purge_batches_claim_order
    ON purge_batches (status, created_at, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_purge_batches_lease
    ON purge_batches (status, lease_expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_purge_batch_items_target
    ON purge_batch_items (target_asset_id, batch_id)
    """,
)


def apply_migration(connection):
    """由显式调用者管理事务；应用启动绝不调用此函数。"""
    for statement in MIGRATION_STATEMENTS:
        connection.execute(text(statement))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        parser.error('必须显式传入 --apply；迁移未执行')

    from app import create_app
    from models import db

    app = create_app()
    with app.app_context(), db.engine.begin() as connection:
        apply_migration(connection)


if __name__ == '__main__':
    main()
