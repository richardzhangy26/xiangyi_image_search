"""Complete catalog contract for the explicit #27 + #28 additive schema."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from sqlalchemy import text

from migrations.issue_27_formal_purge import (
    MIGRATION_STATEMENTS as ISSUE_27_STATEMENTS,
    apply_migration as apply_issue_27,
)
from migrations.issue_28_formal_delete_permits import (
    MIGRATION_STATEMENTS as ISSUE_28_STATEMENTS,
    apply_migration as apply_issue_28,
)


MIGRATION_STATEMENTS = ISSUE_27_STATEMENTS + ISSUE_28_STATEMENTS
REQUIRED_TABLES = (
    "formal_delete_call_permits",
    "formal_deletion_grant_consumptions",
    "object_binding_fences",
    "purge_batch_items",
    "purge_batches",
    "purge_item_events",
    "purge_object_fences",
)
REQUIRED_NAMED_UNIQUES = {
    "uq_formal_delete_permit_item_operation": (
        "batch_id", "target_asset_id", "operation_kind",
    ),
}
_SQL_TYPE = r"uuid|varchar\(\d+\)|text|timestamp|smallint|bigint|integer|boolean"
_SQL_KEYWORDS = {
    "add", "alter", "and", "any", "array", "between", "btree", "character",
    "check", "column", "constraint", "create", "default", "exists", "foreign",
    "if", "in", "index", "is", "key", "not", "null", "on", "or", "primary",
    "references", "table", "unique", "using", "varying", "where",
}


@dataclass(frozen=True)
class PurgeSchemaPlan:
    migration: str
    statement_count: int
    sha256: str


@dataclass(frozen=True)
class PurgeSchemaColumn:
    table: str
    name: str
    data_type: str
    nullable: bool


@dataclass(frozen=True)
class PurgeSchemaDefinition:
    name: str
    definition: str


@dataclass(frozen=True)
class PurgeSchemaSnapshot:
    tables: tuple[str, ...]
    columns: tuple[PurgeSchemaColumn, ...]
    indexes: tuple[PurgeSchemaDefinition, ...]
    constraints: tuple[PurgeSchemaDefinition, ...] = ()


@dataclass(frozen=True)
class PurgeSchemaCheck:
    ready: bool
    missing: tuple[str, ...]
    plan_sha256: str


class PurgeSchemaManager:
    def plan(self):
        canonical = "\n-- statement --\n".join(
            " ".join(statement.split()) for statement in MIGRATION_STATEMENTS
        ).encode("utf-8")
        return PurgeSchemaPlan(
            migration="issue_27_and_28_formal_purge",
            statement_count=len(MIGRATION_STATEMENTS),
            sha256=hashlib.sha256(canonical).hexdigest(),
        )

    def check(self, snapshot: PurgeSchemaSnapshot):
        if not isinstance(snapshot, PurgeSchemaSnapshot):
            raise TypeError("typed purge schema snapshot is required")
        expected = catalog_snapshot_from_statements(MIGRATION_STATEMENTS)
        missing = [table for table in REQUIRED_TABLES if table not in snapshot.tables]
        for table in expected.tables:
            if table not in snapshot.tables and table not in missing:
                missing.append(table)
        actual_columns = {
            (column.table, column.name): column for column in snapshot.columns
        }
        for column in expected.columns:
            actual = actual_columns.get((column.table, column.name))
            if actual is None:
                missing.append(f"{column.table}.{column.name}")
            elif (
                actual.data_type != column.data_type
                or actual.nullable != column.nullable
            ):
                missing.append(f"definition:{column.table}.{column.name}")
        _check_named_definitions(
            snapshot.constraints, expected.constraints, "constraint", missing,
        )
        _check_named_definitions(
            snapshot.indexes, expected.indexes, "index", missing,
        )
        actual_constraints = {
            item.name: _normalize(item.definition) for item in snapshot.constraints
        }
        for name, columns in REQUIRED_NAMED_UNIQUES.items():
            definition = actual_constraints.get(name)
            if definition is None:
                if f"constraint:{name}" not in missing:
                    missing.append(f"constraint:{name}")
            elif _unique_columns(definition) != columns:
                marker = f"definition:constraint:{name}"
                if marker not in missing:
                    missing.append(marker)
        return PurgeSchemaCheck(
            ready=not missing,
            missing=tuple(missing),
            plan_sha256=self.plan().sha256,
        )

    def apply(self, connection, *, expected_plan_sha256, acknowledge_additive):
        if acknowledge_additive is not True:
            raise ValueError("acknowledge_additive is required")
        plan = self.plan()
        if expected_plan_sha256 != plan.sha256:
            raise ValueError("reviewed migration digest mismatch")
        apply_issue_27(connection)
        apply_issue_28(connection)
        return plan


class PostgresPurgeSchemaSnapshotReader:
    def __init__(self, connection):
        self.connection = connection

    def read(self):
        tables = tuple(self.connection.execute(text(
            "SELECT tablename FROM pg_catalog.pg_tables "
            "WHERE schemaname = current_schema() ORDER BY tablename"
        )).scalars().all())
        column_rows = self.connection.execute(text(
            "SELECT table_name, column_name, data_type, udt_name, "
            "character_maximum_length, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "ORDER BY table_name, ordinal_position"
        )).all()
        columns = tuple(PurgeSchemaColumn(
            table=row[0],
            name=row[1],
            data_type=_catalog_type(row[2], row[3], row[4]),
            nullable=row[5] == 'YES',
        ) for row in column_rows)
        constraint_rows = self.connection.execute(text(
            "SELECT c.conname, pg_get_constraintdef(c.oid) "
            "FROM pg_catalog.pg_constraint c "
            "JOIN pg_catalog.pg_class t ON t.oid = c.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = current_schema() ORDER BY c.conname"
        )).all()
        constraints = tuple(PurgeSchemaDefinition(
            name=row[0], definition=_normalize(row[1]),
        ) for row in constraint_rows)
        index_rows = self.connection.execute(text(
            "SELECT indexname, indexdef FROM pg_catalog.pg_indexes "
            "WHERE schemaname = current_schema() ORDER BY indexname"
        )).all()
        indexes = tuple(PurgeSchemaDefinition(
            name=row[0], definition=_normalize(row[1]),
        ) for row in index_rows)
        return PurgeSchemaSnapshot(
            tables=tables,
            columns=columns,
            indexes=indexes,
            constraints=constraints,
        )


def catalog_snapshot_from_statements(statements):
    """Parse PostgreSQL DDL into a catalog snapshot. This is not sqlite."""
    tables = {}
    constraints = {}
    indexes = {}
    for statement in statements:
        normalized = _normalize(statement).rstrip(";")
        if normalized:
            _apply_sql_statement(normalized, tables, constraints, indexes)
    table_names = tuple(sorted(tables))
    return PurgeSchemaSnapshot(
        tables=table_names,
        columns=tuple(
            column for table in table_names for column in tables[table]
        ),
        indexes=tuple(indexes[name] for name in sorted(indexes)),
        constraints=tuple(constraints[name] for name in sorted(constraints)),
    )


def _apply_sql_statement(statement, tables, constraints, indexes):
    create_table = re.match(
        rf"create table if not exists (\w+)\s*\((.*)\)\s*$",
        statement,
    )
    if create_table:
        _parse_create_table(
            create_table.group(1), create_table.group(2), tables, constraints,
        )
        return
    create_index = re.match(
        r"create (unique )?index if not exists (\w+)\s+on (\w+)\s*\((.*?)\)"
        r"(?:\s+where\s+(.*))?\s*$",
        statement,
    )
    if create_index:
        _ensure_table(tables, create_index.group(3))
        name = create_index.group(2)
        indexes[name] = PurgeSchemaDefinition(name=name, definition=statement)
        return
    alter = re.match(r"alter table (\w+)\s+(.*)$", statement)
    if alter:
        _parse_alter(alter.group(1), alter.group(2), tables, constraints)


def _parse_create_table(table, body, tables, constraints):
    _ensure_table(tables, table)
    for clause in _split_comma_clauses(body):
        constraint = re.match(
            r"constraint (\w+)\s+(check|unique)\s*(.*)$",
            clause,
        )
        if constraint:
            constraints[constraint.group(1)] = PurgeSchemaDefinition(
                name=constraint.group(1),
                definition=f"{constraint.group(2)} {constraint.group(3)}".strip(),
            )
            continue
        unnamed_unique = re.match(r"unique\s*\((.*)\)\s*$", clause)
        if unnamed_unique:
            columns = tuple(
                part.strip()
                for part in unnamed_unique.group(1).split(",")
                if part.strip()
            )
            name = f"{table}_{'_'.join(columns)}_key"
            constraints[name] = PurgeSchemaDefinition(
                name=name,
                definition=f"unique ({', '.join(columns)})",
            )
            continue
        column_match = re.match(rf"^(\w+)\s+({_SQL_TYPE})(?=\s|$|,)(.*)$", clause)
        if column_match is None:
            continue
        name, data_type, rest = column_match.groups()
        _add_column(
            tables,
            PurgeSchemaColumn(
                table=table,
                name=name,
                data_type=data_type,
                nullable=_column_nullable(rest),
            ),
        )
        unique_inline = re.search(r"\bunique\b", rest) and not re.search(
            r"\breferences\b", rest,
        )
        if unique_inline:
            key_name = f"{table}_{name}_key"
            constraints[key_name] = PurgeSchemaDefinition(
                name=key_name,
                definition=f"unique ({name})",
            )
        referenced = re.search(r"\breferences\s+(\w+)\s*\((\w+)\)", rest)
        if referenced:
            fk_name = f"{table}_{name}_fkey"
            constraints[fk_name] = PurgeSchemaDefinition(
                name=fk_name,
                definition=(
                    f"foreign key ({name}) references "
                    f"{referenced.group(1)}({referenced.group(2)})"
                ),
            )


def _parse_alter(table, rest, tables, constraints):
    _ensure_table(tables, table)
    dropped = re.match(r"drop constraint if exists (\w+)\s*$", rest)
    if dropped:
        constraints.pop(dropped.group(1), None)
        return
    added = re.match(r"add constraint (\w+)\s+(check\s*\(.*\))\s*$", rest)
    if added:
        constraints[added.group(1)] = PurgeSchemaDefinition(
            name=added.group(1),
            definition=added.group(2),
        )
        return
    for match in re.finditer(
        rf"add column if not exists (\w+)\s+({_SQL_TYPE})(?=\s|,|$)(.*?)(?=(?:,\s*add column)|\Z)",
        rest,
    ):
        name, data_type, tail = match.groups()
        _add_column(
            tables,
            PurgeSchemaColumn(
                table=table,
                name=name,
                data_type=data_type,
                nullable=_column_nullable(tail),
            ),
        )


def _ensure_table(tables, table):
    tables.setdefault(table, [])


def _add_column(tables, column):
    existing = tables.setdefault(column.table, [])
    if any(item.name == column.name for item in existing):
        return
    existing.append(column)


def _column_nullable(rest):
    return not (
        re.search(r"\bnot null\b", rest) or re.search(r"\bprimary key\b", rest)
    )


def _split_comma_clauses(body):
    parts = []
    current = []
    depth = 0
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            clause = "".join(current).strip()
            if clause:
                parts.append(clause)
            current = []
            continue
        current.append(char)
    clause = "".join(current).strip()
    if clause:
        parts.append(clause)
    return parts


def _catalog_type(data_type, udt_name, length):
    if data_type == 'character varying':
        return f'varchar({length})' if length else 'varchar'
    if data_type == 'timestamp without time zone':
        return 'timestamp'
    return {
        'uuid': 'uuid', 'text': 'text', 'smallint': 'smallint',
        'bigint': 'bigint', 'integer': 'integer', 'boolean': 'boolean',
    }.get(data_type, str(udt_name).lower())


def _check_named_definitions(actual_values, expected_values, kind, missing):
    actual = {value.name: _normalize(value.definition) for value in actual_values}
    for expected in expected_values:
        definition = actual.get(expected.name)
        if definition is None:
            missing.append(f"{kind}:{expected.name}")
        elif not _definition_matches(expected.definition, definition):
            missing.append(f"definition:{kind}:{expected.name}")


def _definition_matches(expected_definition, actual_normalized):
    expected_norm = _normalize(expected_definition)
    if expected_norm in actual_normalized or actual_normalized in expected_norm:
        return True
    expected_unique = _unique_columns(expected_norm)
    if expected_unique is not None:
        return expected_unique == _unique_columns(actual_normalized)
    return _significant_tokens(expected_norm) <= _significant_tokens(actual_normalized)


def _unique_columns(definition):
    match = re.search(r"\bunique\s*\((.*?)\)", _normalize(definition))
    if match is None:
        return None
    return tuple(
        part.strip() for part in match.group(1).split(",") if part.strip()
    )


def _significant_tokens(definition):
    tokens = set()
    for quoted in re.findall(r"'([^']*)'", definition):
        tokens.add(quoted.lower())
    remainder = re.sub(r"'[^']*'", " ", definition)
    remainder = re.sub(r"::[a-z_]+", " ", remainder)
    for token in re.findall(r"[a-z_][a-z0-9_]*|\d+", remainder.lower()):
        if token not in _SQL_KEYWORDS:
            tokens.add(token)
    return tokens


def _normalize(value):
    return re.sub(r"\s+", " ", str(value).strip().lower())
