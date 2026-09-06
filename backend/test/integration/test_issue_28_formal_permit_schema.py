import uuid
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from models import (
    FormalDeleteCallPermit,
    FormalDeletionGrantConsumption,
    db,
)


def _grant(*, grant_id=None, batch_id=None):
    now = datetime.now()
    return FormalDeletionGrantConsumption(
        grant_id=grant_id or f'grant-{uuid.uuid4()}',
        batch_id=batch_id or uuid.uuid4(),
        environment_id='test', deployment_sha256='a' * 64,
        database_manifest_sha256='b' * 64,
        object_manifest_sha256='c' * 64,
        formal_bucket='formal-test-bucket', asset_scope_sha256='d' * 64,
        max_assets=1, max_object_deletes=2, used_object_deletes=0,
        issued_at=now, expires_at=now + timedelta(minutes=10),
        consumed_at=now, state='active', trust_attestation_sha256='e' * 64,
        audit_retain_until=now + timedelta(days=365),
    )


def _permit(grant, *, operation='original'):
    now = datetime.now()
    return FormalDeleteCallPermit(
        id=uuid.uuid4(), grant_id=grant.grant_id, batch_id=grant.batch_id,
        target_asset_id=uuid.uuid4(), operation_kind=operation,
        claim_generation=1, formal_bucket=grant.formal_bucket,
        formal_key=f'{operation}/asset', object_size=1,
        object_sha256='f' * 64, object_etag='etag',
        original_fence_id=uuid.uuid4(), preview_fence_id=uuid.uuid4(),
        state='issued', issued_at=now, expires_at=grant.expires_at,
        audit_retain_until=now + timedelta(days=365),
    )


def test_grant_is_unique_per_id_and_batch_and_permit_per_item_operation(app):
    grant = _grant()
    permit = _permit(grant)
    db.session.add(grant)
    db.session.flush()
    db.session.add(permit)
    db.session.commit()

    db.session.add(_grant(grant_id=grant.grant_id, batch_id=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()

    duplicate = _permit(grant)
    duplicate.target_asset_id = permit.target_asset_id
    db.session.add(duplicate)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_complete_postgres_catalog_is_ready_and_wrong_column_definition_fails(app):
    """ORM create_all 只证明 ORM 能建表，不能当作生产 catalog 证据。"""
    from services.purge_schema_manager import (
        PostgresPurgeSchemaSnapshotReader,
        PurgeSchemaManager,
    )

    snapshot = PostgresPurgeSchemaSnapshotReader(db.session.connection()).read()
    manager = PurgeSchemaManager()
    check = manager.check(snapshot)
    assert check.ready is True, check.missing
    assert any(
        item.name == 'uq_formal_delete_permit_item_operation'
        for item in snapshot.constraints
    )

    changed = tuple(
        replace(column, data_type='text')
        if (
            column.table == 'formal_delete_call_permits'
            and column.name == 'formal_bucket'
        )
        else column
        for column in snapshot.columns
    )
    rejected = manager.check(replace(snapshot, columns=changed))
    assert rejected.ready is False
    assert 'definition:formal_delete_call_permits.formal_bucket' in rejected.missing


def test_migration_sql_on_empty_schema_is_ready_and_rejects_named_unique_drift(
    _test_database,
):
    """对空临时 schema 执行 migration SQL，不用 ORM create_all 自证 ready。"""
    import secrets

    import sqlalchemy
    from sqlalchemy import text

    from migrations.issue_27_formal_purge import apply_migration as apply_issue_27
    from migrations.issue_28_formal_delete_permits import (
        apply_migration as apply_issue_28,
    )
    from services.purge_schema_manager import (
        PostgresPurgeSchemaSnapshotReader,
        PurgeSchemaManager,
    )

    schema_name = f't14_b_{secrets.token_hex(8)}'
    quoted_schema = f'"{schema_name}"'
    engine = sqlalchemy.create_engine(_test_database, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            try:
                connection.execute(text(f'CREATE SCHEMA {quoted_schema}'))
                connection.execute(text(f'SET search_path TO {quoted_schema}'))
                connection.execute(text(
                    'CREATE TABLE purge_batches ('
                    'id UUID PRIMARY KEY, '
                    "status VARCHAR(24) NOT NULL DEFAULT 'queued', "
                    'claim_generation BIGINT NOT NULL DEFAULT 0)'
                ))
                connection.execute(text(
                    'CREATE TABLE purge_batch_items ('
                    'batch_id UUID NOT NULL, '
                    'target_asset_id UUID NOT NULL, '
                    'ordinal SMALLINT NOT NULL DEFAULT 0, '
                    "status VARCHAR(24) NOT NULL DEFAULT 'pending', "
                    'PRIMARY KEY (batch_id, target_asset_id))'
                ))
                apply_issue_27(connection)
                apply_issue_28(connection)
                connection.commit()

                snapshot = PostgresPurgeSchemaSnapshotReader(connection).read()
                manager = PurgeSchemaManager()
                check = manager.check(snapshot)
                assert check.ready is True, check.missing
                assert any(
                    item.name == 'uq_formal_delete_permit_item_operation'
                    for item in snapshot.constraints
                )

                changed = tuple(
                    replace(column, data_type='text')
                    if (
                        column.table == 'formal_delete_call_permits'
                        and column.name == 'formal_bucket'
                    )
                    else column
                    for column in snapshot.columns
                )
                rejected_type = manager.check(replace(snapshot, columns=changed))
                assert rejected_type.ready is False
                assert (
                    'definition:formal_delete_call_permits.formal_bucket'
                    in rejected_type.missing
                )

                dropped_unique = tuple(
                    item
                    for item in snapshot.constraints
                    if item.name != 'uq_formal_delete_permit_item_operation'
                )
                rejected_unique = manager.check(
                    replace(snapshot, constraints=dropped_unique)
                )
                assert rejected_unique.ready is False
                assert (
                    'constraint:uq_formal_delete_permit_item_operation'
                    in rejected_unique.missing
                )
            finally:
                connection.rollback()
                connection.execute(text(f'DROP SCHEMA IF EXISTS {quoted_schema} CASCADE'))
                connection.commit()
    finally:
        engine.dispose()
