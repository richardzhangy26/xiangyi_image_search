"""Issue #26 batch schema 的静态与 ORM 合同。"""

from pathlib import Path

import pytest
from sqlalchemy import UniqueConstraint, create_engine

from models import db


BACKEND = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (BACKEND / relative_path).read_text(encoding="utf-8").lower()


def test_migration_keeps_tombstone_item_id_and_complete_batch_outcomes():
    source = _read("migrations/issue_26_purge_batches.py")

    assert "create table if not exists purge_batches" in source
    assert "create table if not exists purge_batch_items" in source
    assert "target_asset_id uuid not null" in source
    assert "references image_assets" not in source
    assert "database_backup_id" in source
    assert "database_manifest_sha256" in source
    assert "object_manifest_sha256" in source
    assert "retain_until" in source
    assert "error_code" in source
    assert "result_code" in source
    assert "checkpoint_at" in source
    assert "status in ('queued', 'database_backup', 'object_backup', 'verifying', 'pending_deletion', 'failed', 'cancelled')" in source
    assert "jsonb" not in source


def test_first_bootstrap_schema_matches_explicit_batch_migration_contract():
    source = (BACKEND.parent / "postgres/init/01_init.sql").read_text(
        encoding="utf-8"
    ).lower()

    assert "create table if not exists purge_batches" in source
    assert "create table if not exists purge_batch_items" in source
    assert "target_asset_id uuid not null" in source
    assert "target_asset_id uuid references image_assets" not in source
    assert "idx_purge_batches_claim_order" in source


def test_orm_schema_exposes_complete_item_outcomes_and_unique_idempotency_pair():
    from models.purge_batch import PurgeBatch, PurgeBatchItem

    batch_columns = set(PurgeBatch.__table__.c.keys())
    item_columns = set(PurgeBatchItem.__table__.c.keys())
    assert {
        "database_backup_id",
        "database_manifest_sha256",
        "object_manifest_sha256",
        "retain_until",
        "error_code",
        "created_at",
        "started_at",
        "completed_at",
        "failed_at",
        "cancelled_at",
    } <= batch_columns
    assert {"batch_id", "target_asset_id", "status", "result_code", "error_code", "checkpoint_at"} <= item_columns
    assert "asset_id" not in item_columns
    assert any(
        set(constraint.columns.keys()) == {"actor_id", "idempotency_key"}
        for constraint in PurgeBatch.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    )


def test_models_create_on_sqlite_without_requiring_asset_foreign_key():
    from models.purge_batch import PurgeBatch, PurgeBatchItem

    engine = create_engine("sqlite://")
    PurgeBatch.__table__.create(engine)
    PurgeBatchItem.__table__.create(engine)
    assert "target_asset_id" in PurgeBatchItem.__table__.primary_key.columns
