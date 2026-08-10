"""Issue #22 保留期/清理的静态安全合同。"""

from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8') if path.exists() else ''


def test_orm_and_schemas_reach_seven_state_terminal_with_cleanup_fields():
    orm = _read(BACKEND_DIR / 'models' / 'image_import_item.py')
    fresh = _read(ROOT_DIR / 'postgres' / 'init' / '01_init.sql')

    for source in (orm, fresh):
        assert "'awaiting_retry', 'cancelled', 'abandoned'" in source

    for field in ('purge_eligible_at', 'objects_purged_at'):
        assert f'{field} =' in orm
        assert field in fresh

    assert "'purge_eligible_at'" in orm.split('def to_public_dict')[1]
    assert "'objects_purged_at'" in orm.split('def to_public_dict')[1]
    assert 'idx_image_import_items_purge_schedule' in fresh


def test_issue_22_migration_is_expand_only_idempotent_and_explicit():
    migration = _read(
        BACKEND_DIR / 'migrations' / 'issue_22_retention_cleanup.py'
    )

    assert 'ADD COLUMN IF NOT EXISTS purge_eligible_at TIMESTAMP' in migration
    assert 'ADD COLUMN IF NOT EXISTS objects_purged_at TIMESTAMP' in migration
    assert "'abandoned'" in migration
    assert 'ck_image_import_items_status_v2' in migration
    assert 'idx_image_import_items_purge_schedule' in migration
    assert 'def apply_migration(connection)' in migration
    assert "parser.add_argument('--apply', action='store_true')" in migration

    lowered = migration.lower()
    assert 'drop table' not in lowered
    assert 'delete from' not in lowered


def test_issue_22_migration_is_never_implicit():
    migration_name = 'issue_22_retention_cleanup'
    app = _read(BACKEND_DIR / 'app.py')
    worker = _read(BACKEND_DIR / 'scripts' / 'run_image_import_worker.py')

    assert migration_name not in app
    assert migration_name not in worker


def test_cleanup_service_is_reference_safe_and_checkpointed():
    cleanup = _read(BACKEND_DIR / 'services' / 'import_cleanup.py')

    assert 'def count_object_references(' in cleanup
    assert 'def cleanup_one_item(' in cleanup
    assert 'def cleanup_expired_imports(' in cleanup
    # 引用来源必须同时覆盖正式资产与导入项
    assert 'ImageAsset' in cleanup
    assert 'ImageImportItem' in cleanup
    assert 'objects_purged_at' in cleanup
    # 清理结果写审计
    assert "event_type='image_import.objects_purged'" in cleanup
    assert "event_type='image_import.expired'" in cleanup
    # 回收站资产的引用即保护：archived 资产参与引用计数
    assert 'asset_column == key' in cleanup


def test_cleanup_runner_is_env_gated_and_graceful():
    runner = _read(BACKEND_DIR / 'scripts' / 'run_import_cleanup.py')

    assert "IMPORT_CLEANUP_ENABLED" in runner
    assert "'true'" in runner or '"true"' in runner
    assert 'signal.SIGTERM' in runner
    assert 'signal.SIGINT' in runner
    # 绝不引入 Kodo/Qiniu 来源模块
    assert 'kodo_source' not in runner
    assert 'qiniu' not in runner.lower()


def test_storage_delete_adapter_is_structured_and_idempotent():
    storage = _read(BACKEND_DIR / 'services' / 'object_storage.py')

    assert 'def delete_object(' in storage
    assert "'already_gone'" in storage
    assert "'deleted'" in storage
    assert "error.stage = 'delete'" in storage


def test_cleanup_compose_service_is_isolated_behind_profile_and_env():
    compose = _read(ROOT_DIR / 'docker-compose.yml')

    assert '  cleanup:' in compose
    assert 'profiles: ["cleanup"]' in compose
    assert 'IMPORT_CLEANUP_ENABLED=${IMPORT_CLEANUP_ENABLED:-false}' in compose
    assert 'command: ["python", "-m", "scripts.run_import_cleanup"]' in compose


def test_restore_and_abandon_routes_are_registered():
    source = _read(BACKEND_DIR / 'blueprints' / 'image_imports.py')

    assert "@image_imports_bp.post('/<uuid:item_id>/restore')" in source
    assert "@image_imports_bp.post('/<uuid:item_id>/abandon')" in source
    assert 'IMAGE_IMPORT_RESTORE_WINDOW_EXPIRED' in source
    assert 'IMAGE_IMPORT_ABANDON_NOT_ALLOWED' in source
    # 恢复/放弃不删除对象；对象删除只在受控清理路径
    lowered = source.lower()
    assert 'delete_object' not in lowered
