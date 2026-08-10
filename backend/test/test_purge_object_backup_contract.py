import ast
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
REPOSITORY = BACKEND.parent
PRODUCTION_FILES = (
    BACKEND / "services" / "purge_object_backup.py",
    BACKEND / "services" / "purge_object_restore.py",
    BACKEND / "services" / "purge_object_storage.py",
    BACKEND / "scripts" / "manage_purge_object_backups.py",
)


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_purge_object_modules_have_no_delete_or_broad_cloud_capabilities():
    forbidden_calls = {
        "delete_object",
        "delete_objects",
        "batch_delete",
        "sign_url",
        "sign_download_url",
        "list_objects",
        "list_objects_v2",
        "put_bucket_acl",
        "put_bucket_lifecycle",
    }

    for path in PRODUCTION_FILES:
        called = {
            node.func.attr
            for node in ast.walk(_tree(path))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        assert called.isdisjoint(forbidden_calls), (path, called & forbidden_calls)


def test_purge_object_modules_are_not_wired_into_app_or_database_models():
    forbidden_roots = {"flask", "sqlalchemy", "models", "app"}
    for path in PRODUCTION_FILES:
        imported = set()
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.lstrip(".").split(".")[0])
        assert imported.isdisjoint(forbidden_roots), (
            path,
            imported & forbidden_roots,
        )
        source = path.read_text(encoding="utf-8")
        assert "DATABASE_URL" not in source
        assert "PurgeObjectBackupService" not in (
            source if path.name == "manage_purge_object_backups.py" else ""
        )


def test_ops_credentials_are_not_injected_into_daily_application_runtime():
    forbidden = ("BACKUP_OSS_", "PURGE_SOURCE_OSS_", "PURGE_RESTORE_OSS_")
    for path in (
        BACKEND / "app.py",
        BACKEND / ".env.example",
        REPOSITORY / "docker-compose.yml",
    ):
        source = path.read_text(encoding="utf-8")
        assert all(name not in source for name in forbidden), path

    ops_example = (BACKEND / ".env.backup.example").read_text(
        encoding="utf-8"
    )
    assert "PURGE_RESTORE_ISOLATED=0" in ops_example
    assert "PURGE_SOURCE_OSS_ACCESS_KEY_ID=" in ops_example
    assert "PURGE_RESTORE_OSS_ACCESS_KEY_ID=" in ops_example
