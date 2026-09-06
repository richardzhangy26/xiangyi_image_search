from dataclasses import replace


def test_schema_plan_is_stable_and_empty_snapshot_fails_closed():
    from services.purge_schema_manager import PurgeSchemaManager, PurgeSchemaSnapshot

    manager = PurgeSchemaManager()
    plan = manager.plan()
    assert plan.migration == "issue_27_and_28_formal_purge"
    assert plan.statement_count > 0
    assert len(plan.sha256) == 64

    check = manager.check(PurgeSchemaSnapshot(tables=(), columns=(), indexes=()))
    assert check.ready is False
    assert "purge_batch_items.formal_bucket" in check.missing
    assert "index:uq_purge_object_fences_held_identity" in check.missing


def test_schema_apply_requires_ack_and_exact_plan_digest_before_fake_execution():
    from services.purge_schema_manager import PurgeSchemaManager

    class Connection:
        def __init__(self):
            self.statements = []

        def execute(self, statement):
            self.statements.append(str(statement))

    manager = PurgeSchemaManager()
    connection = Connection()
    try:
        manager.apply(
            connection,
            expected_plan_sha256=manager.plan().sha256,
            acknowledge_additive=False,
        )
    except ValueError as exc:
        assert "acknowledge" in str(exc)
    else:
        raise AssertionError("schema apply must require explicit acknowledgement")
    assert connection.statements == []

    try:
        manager.apply(
            connection,
            expected_plan_sha256="0" * 64,
            acknowledge_additive=True,
        )
    except ValueError as exc:
        assert "digest" in str(exc)
    else:
        raise AssertionError("schema apply must bind the reviewed plan digest")
    assert connection.statements == []

    manager.apply(
        connection,
        expected_plan_sha256=manager.plan().sha256,
        acknowledge_additive=True,
    )
    assert len(connection.statements) == manager.plan().statement_count


def test_migration_sql_snapshot_is_ready_without_orm_create_all():
    from services.purge_schema_manager import (
        MIGRATION_STATEMENTS,
        PurgeSchemaManager,
        catalog_snapshot_from_statements,
    )

    snapshot = catalog_snapshot_from_statements(MIGRATION_STATEMENTS)
    check = PurgeSchemaManager().check(snapshot)
    assert check.ready is True, check.missing
    assert any(
        item.name == 'uq_formal_delete_permit_item_operation'
        for item in snapshot.constraints
    )


def test_check_rejects_wrong_column_type_and_missing_named_unique():
    from services.purge_schema_manager import (
        MIGRATION_STATEMENTS,
        PurgeSchemaManager,
        catalog_snapshot_from_statements,
    )

    manager = PurgeSchemaManager()
    snapshot = catalog_snapshot_from_statements(MIGRATION_STATEMENTS)
    assert manager.check(snapshot).ready is True

    changed_columns = tuple(
        replace(column, data_type='text')
        if (
            column.table == 'formal_delete_call_permits'
            and column.name == 'formal_bucket'
        )
        else column
        for column in snapshot.columns
    )
    rejected_type = manager.check(replace(snapshot, columns=changed_columns))
    assert rejected_type.ready is False
    assert 'definition:formal_delete_call_permits.formal_bucket' in rejected_type.missing

    dropped_unique = tuple(
        item
        for item in snapshot.constraints
        if item.name != 'uq_formal_delete_permit_item_operation'
    )
    rejected_unique = manager.check(replace(snapshot, constraints=dropped_unique))
    assert rejected_unique.ready is False
    assert 'constraint:uq_formal_delete_permit_item_operation' in rejected_unique.missing


def test_schema_cli_plan_is_offline_and_apply_requires_all_explicit_gates():
    import json
    from io import StringIO

    from scripts.manage_purge_schema import main

    connections = []
    output = StringIO()
    assert main(
        ["plan"],
        connection_factory=lambda: connections.append("connected"),
        stdout=output,
    ) == 0
    assert json.loads(output.getvalue())["status"] == "planned"
    assert connections == []

    denied = StringIO()
    assert main(
        ["apply", "--expected-plan-sha256", "0" * 64],
        connection_factory=lambda: connections.append("connected"),
        stderr=denied,
    ) == 2
    assert json.loads(denied.getvalue())["error_code"] == "PURGE_SCHEMA_APPLY_NOT_ACKNOWLEDGED"
    assert connections == []
