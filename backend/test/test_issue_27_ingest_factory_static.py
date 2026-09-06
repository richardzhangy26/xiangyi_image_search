from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_asset_ingest_accepts_caller_owned_control_session_factory():
    source = (ROOT / 'backend/services/asset_ingest.py').read_text(encoding='utf-8')
    assert 'control_session_factory' in source


def test_control_session_factory_is_optional_for_legacy_ingest_path():
    from services.asset_ingest import ImageAssetIngestService
    assert 'control_session_factory' in ImageAssetIngestService.__init__.__annotations__
