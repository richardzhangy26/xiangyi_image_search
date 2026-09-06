"""Issue #15 static contracts that never connect to PostgreSQL."""

from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def test_schema_sources_define_display_name_version_and_activity_records():
    image_model = _read(BACKEND_DIR / 'models' / 'image_asset.py')
    activity_model = _read(
        BACKEND_DIR / 'models' / 'asset_activity_record.py'
    )
    init_sql = _read(REPO_DIR / 'postgres' / 'init' / '01_init.sql')

    assert 'display_name = db.Column(' in image_model
    assert 'db.Text,\n        nullable=False,' in image_model
    assert "version = db.Column(db.BigInteger, nullable=False" in image_model
    assert "'version >= 1'" in image_model
    assert "__tablename__ = 'asset_activity_records'" in activity_model
    assert 'ForeignKey' not in activity_model
    assert 'display_name        TEXT NOT NULL' in init_sql
    assert 'version             BIGINT NOT NULL DEFAULT 1' in init_sql
    assert 'CREATE TABLE IF NOT EXISTS asset_activity_records' in init_sql
    assert 'idx_asset_activity_target_created' in init_sql
    assert 'idx_asset_activity_request_id' in init_sql


def test_migration_is_explicit_idempotent_and_not_wired_to_startup():
    migration = _read(
        BACKEND_DIR / 'migrations' / 'issue_15_asset_display_name.py'
    )
    app_source = _read(BACKEND_DIR / 'app.py')
    init_source = _read(BACKEND_DIR / 'init_db.py')

    assert 'def apply_migration(connection)' in migration
    assert 'ADD COLUMN IF NOT EXISTS display_name' in migration
    assert 'ADD COLUMN IF NOT EXISTS version' in migration
    assert 'WHERE display_name IS NULL' in migration
    assert 'WHERE version IS NULL' in migration
    assert 'CREATE OR REPLACE FUNCTION set_image_asset_display_name' in migration
    assert "parser.add_argument('--apply', action='store_true')" in migration
    assert 'issue_15_asset_display_name' not in app_source
    assert 'DISPLAY_NAME_TRIGGER_STATEMENTS' in init_source
    assert 'apply_migration(' not in init_source


def test_search_and_representations_expose_both_names_and_version():
    management = _read(BACKEND_DIR / 'blueprints' / 'image_assets.py')
    naming_service = _read(BACKEND_DIR / 'services' / 'asset_display_name.py')
    vector_search = _read(BACKEND_DIR / 'services' / 'vector_search.py')
    products = _read(BACKEND_DIR / 'blueprints' / 'products_v2.py')

    assert 'ImageAsset.display_name.ilike' in management
    assert 'ImageAsset.source_relative_path.ilike' in management
    assert "'display_name':" in naming_service
    assert "'source_relative_path':" in naming_service
    assert "'version':" in naming_service
    assert 'display_name,' in vector_search
    assert 'source_relative_path,' in vector_search
    assert 'version,' in vector_search
    assert "'display_name': row.display_name" in vector_search
    assert "'source_relative_path': row.source_relative_path" in vector_search
    assert "'version': row.version" in vector_search
    assert "'display_name': asset.display_name" in products
    assert "'version': asset.version" in products


def test_rename_route_uses_dedicated_service_and_stable_contract():
    management = _read(BACKEND_DIR / 'blueprints' / 'image_assets.py')
    naming_service = _read(BACKEND_DIR / 'services' / 'asset_display_name.py')

    assert "@image_assets_bp.post('/<uuid:asset_id>/rename')" in management
    assert 'rename_image_asset(' in management
    assert 'IMAGE_ASSET_VERSION_CONFLICT' in naming_service
    assert 'IMAGE_ASSET_NOT_ACTIVE' in naming_service
    assert 'update(ImageAsset)' in naming_service
    assert 'ImageAsset.version == expected_version' in naming_service
    assert 'version=ImageAsset.version + 1' in naming_service
    assert 'AssetActivityRecord(' in naming_service
    assert 'session.commit()' in naming_service
    assert 'session.rollback()' in naming_service
