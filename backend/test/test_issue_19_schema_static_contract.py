"""Issue #19 持久图片导入项的静态 schema 安全合同。"""

from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8') if path.exists() else ''


def test_import_item_orm_has_persistent_states_identity_and_claim_fence():
    source = _read(BACKEND_DIR / 'models' / 'image_import_item.py')

    assert "__tablename__ = 'image_import_items'" in source
    # Issue #20+#21 汇合：状态集合扩展为六状态超集（仍包含原四状态）
    assert "'queued', 'embedding', 'completed', 'failed'," in source
    assert "'awaiting_retry'" in source
    assert "'cancelled'" in source
    assert "name='uq_image_import_items_source_identity'" in source
    for field in (
        'source_provider',
        'source_bucket',
        'source_relative_path',
        'source_revision',
        'expected_embedding_model',
        'expected_embedding_dimension',
        'asset_id',
        'claim_token',
        'claim_generation',
        'claimed_by',
        'lease_expires_at',
        'embedding_started_at',
        'completed_at',
        'failed_at',
        'failure_message',
        'created_at',
        'updated_at',
    ):
        assert f'{field} =' in source

    assert "default='tongyi-embedding-vision-plus-2026-03-06'" in source
    assert 'default=1024' in source
    assert "name='ck_image_import_items_embedding_dimension'" in source
    assert "name='ck_image_import_items_embedding_model'" in source
    assert "db.ForeignKey('image_assets.id', ondelete='SET NULL')" in source
    assert 'nullable=True' in source
    assert "db.Index('idx_image_import_items_claim_order'" in source
    assert "db.Index('idx_image_import_items_lease'" in source


def test_fresh_schema_and_explicit_migration_define_the_same_expand_only_table():
    fresh = _read(ROOT_DIR / 'postgres' / 'init' / '01_init.sql')
    migration = _read(
        BACKEND_DIR / 'migrations' / 'issue_19_image_import_items.py'
    )

    # Issue #20+#21+#22 汇合：新装 schema 使用七状态超集；#19 历史迁移保留四状态创建步骤
    assert (
        "status IN ('queued', 'embedding', 'completed', 'failed',"
        " 'awaiting_retry', 'cancelled', 'abandoned')" in fresh
    )
    assert "status IN ('queued', 'embedding', 'completed', 'failed')" in migration

    for source in (fresh, migration):
        assert 'CREATE TABLE IF NOT EXISTS image_import_items' in source
        assert 'uq_image_import_items_source_identity' in source
        assert 'ck_image_import_items_embedding_model' in source
        assert 'ck_image_import_items_embedding_dimension' in source
        assert 'idx_image_import_items_claim_order' in source
        assert 'idx_image_import_items_lease' in source

    lowered = migration.lower()
    assert 'def apply_migration(connection)' in migration
    assert "parser.add_argument('--apply', action='store_true')" in migration
    assert 'drop table' not in lowered
    assert 'delete from' not in lowered


def test_migration_is_never_implicit_in_app_or_worker_entrypoint():
    migration_name = 'issue_19_image_import_items'
    app = _read(BACKEND_DIR / 'app.py')
    worker = _read(BACKEND_DIR / 'scripts' / 'run_image_import_worker.py')

    assert migration_name not in app
    assert migration_name not in worker

