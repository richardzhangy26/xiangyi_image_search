"""Issue #20 重试 schema 与迁移的静态安全合同。"""

from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8') if path.exists() else ''


def test_orm_adds_retry_fields_and_five_state_superset():
    source = _read(BACKEND_DIR / 'models' / 'image_import_item.py')

    for field in (
        'attempt_count',
        'last_error_class',
        'last_attempt_at',
        'next_retry_at',
    ):
        assert f'{field} =' in source

    assert "'awaiting_retry'" in source
    assert "'queued', 'embedding', 'completed', 'failed'," in source
    assert "name='ck_image_import_items_status_v2'" in source
    assert "name='ck_image_import_items_attempt_count'" in source
    assert "db.Index(" in source
    assert "'idx_image_import_items_retry_schedule'" in source

    # 公开响应携带重试诊断字段，但不暴露领取栅栏或私有对象键
    assert "'attempt_count': self.attempt_count" in source
    assert "'max_auto_attempts': MAX_AUTO_ATTEMPTS" in source
    assert "'last_error_class': self.last_error_class" in source
    assert "'next_retry_at'" in source
    assert 'claim_token' not in source.split('def to_public_dict')[1]
    assert 'oss_path' not in source.split('def to_public_dict')[1]


def test_issue_20_migration_is_expand_only_idempotent_and_explicit():
    migration = _read(BACKEND_DIR / 'migrations' / 'issue_20_retry_backoff.py')

    for column in (
        'ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0',
        'ADD COLUMN IF NOT EXISTS last_error_class VARCHAR(32)',
        'ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMP',
        'ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMP',
    ):
        assert column in migration

    assert 'ck_image_import_items_attempt_count' in migration
    assert 'ck_image_import_items_status_v2' in migration
    assert "'awaiting_retry'" in migration
    assert 'idx_image_import_items_retry_schedule' in migration
    assert 'def apply_migration(connection)' in migration
    assert "parser.add_argument('--apply', action='store_true')" in migration

    lowered = migration.lower()
    assert 'drop table' not in lowered
    assert 'delete from' not in lowered


def test_fresh_schema_matches_issue_20_final_state():
    fresh = _read(ROOT_DIR / 'postgres' / 'init' / '01_init.sql')

    assert "attempt_count                INTEGER NOT NULL DEFAULT 0" in fresh
    assert 'last_error_class' in fresh
    assert 'last_attempt_at' in fresh
    assert 'next_retry_at' in fresh
    # Issue #20–#22 汇合：新装 schema 的状态超集最终含七状态
    assert (
        "status IN ('queued', 'embedding', 'completed', 'failed',"
        " 'awaiting_retry', 'cancelled', 'abandoned')" in fresh
    )
    assert 'ck_image_import_items_status_v2' in fresh
    assert 'ck_image_import_items_attempt_count' in fresh
    assert 'idx_image_import_items_retry_schedule' in fresh


def test_issue_20_migration_is_never_implicit_in_app_or_worker_entrypoint():
    migration_name = 'issue_20_retry_backoff'
    app = _read(BACKEND_DIR / 'app.py')
    worker = _read(BACKEND_DIR / 'scripts' / 'run_image_import_worker.py')

    assert migration_name not in app
    assert migration_name not in worker
