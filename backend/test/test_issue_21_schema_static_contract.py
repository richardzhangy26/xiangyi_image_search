"""Issue #21 取消 schema 与迁移的静态安全合同。"""

from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8') if path.exists() else ''


def test_orm_adds_cancel_intent_fields_and_cancelled_terminal_state():
    source = _read(BACKEND_DIR / 'models' / 'image_import_item.py')

    for field in (
        'cancel_requested_at',
        'cancel_requested_by',
        'cancelled_at',
    ):
        assert f'{field} =' in source

    assert "'cancelled'" in source
    assert "'queued', 'embedding', 'completed', 'failed'," in source
    assert "name='ck_image_import_items_status_v2'" in source
    # Issue #20+#21 汇合：等待重试项同样可取消
    assert (
        "CANCELABLE_STATUSES = ('queued', 'embedding', 'failed',"
        " 'awaiting_retry')" in source
    )

    public_scope = source.split('def to_public_dict')[1]
    assert "'cancel_requested_at'" in public_scope
    assert "'cancelled_at'" in public_scope
    assert 'claim_token' not in public_scope
    assert 'oss_path' not in public_scope


def test_issue_21_migration_is_expand_only_idempotent_and_explicit():
    migration = _read(BACKEND_DIR / 'migrations' / 'issue_21_import_cancel.py')

    for column in (
        'ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMP',
        'ADD COLUMN IF NOT EXISTS cancel_requested_by VARCHAR(128)',
        'ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP',
    ):
        assert column in migration

    assert 'ck_image_import_items_status_v2' in migration
    assert "'cancelled'" in migration
    # Issue #20+#21 汇合：迁移重建六状态超集约束
    assert "'awaiting_retry'" in migration
    assert 'def apply_migration(connection)' in migration
    assert "parser.add_argument('--apply', action='store_true')" in migration

    lowered = migration.lower()
    assert 'drop table' not in lowered
    assert 'delete from' not in lowered


def test_fresh_schema_matches_issue_21_final_state():
    fresh = _read(ROOT_DIR / 'postgres' / 'init' / '01_init.sql')

    assert 'cancel_requested_at' in fresh
    assert 'cancel_requested_by' in fresh
    assert 'cancelled_at' in fresh
    # Issue #20+#21 汇合：新装 schema 的状态超集同时含 awaiting_retry 与 cancelled
    assert (
        "status IN ('queued', 'embedding', 'completed', 'failed',"
        " 'awaiting_retry', 'cancelled')" in fresh
    )
    assert 'ck_image_import_items_status_v2' in fresh


def test_issue_21_migration_is_never_implicit_in_app_or_worker_entrypoint():
    migration_name = 'issue_21_import_cancel'
    app = _read(BACKEND_DIR / 'app.py')
    worker = _read(BACKEND_DIR / 'scripts' / 'run_image_import_worker.py')

    assert migration_name not in app
    assert migration_name not in worker
