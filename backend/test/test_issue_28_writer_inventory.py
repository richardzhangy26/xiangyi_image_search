import pytest


def test_formal_deployment_refuses_legacy_unfenced_http_writers():
    from services.fence_composition import binding_fence_kwargs

    with pytest.raises(ValueError, match="binding fence"):
        binding_fence_kwargs({
            "PURGE_FORMAL_DELETION_DEPLOYED": "1",
            "INGEST_BINDING_FENCE_ENABLED": "0",
            "OSS_BUCKET_NAME": "formal-images-private",
        })


def test_writer_inventory_is_fixed_and_all_http_factories_use_shared_composition():
    from pathlib import Path
    from services.fence_composition import (
        FORMAL_OBJECT_WRITER_INVENTORY,
        formal_writer_inventory_sha256,
    )

    assert FORMAL_OBJECT_WRITER_INVENTORY == (
        "http:image_assets.import",
        "http:image_imports.queue",
        "http:products.create_update",
        "operator:kodo_migration",
        "worker:image_import_promotion",
        "worker:import_cleanup",
    )
    assert len(formal_writer_inventory_sha256()) == 64
    backend = Path(__file__).resolve().parents[1]
    for relative in (
        "blueprints/image_assets.py",
        "blueprints/image_imports.py",
        "blueprints/products_v2.py",
    ):
        source = (backend / relative).read_text(encoding="utf-8")
        assert "request_fence_kwargs()" in source


def test_formal_deployment_fails_during_app_startup_before_any_request(monkeypatch):
    from app import create_app

    monkeypatch.setenv('PURGE_FORMAL_DELETION_DEPLOYED', '1')
    monkeypatch.setenv('INGEST_BINDING_FENCE_ENABLED', '0')
    with pytest.raises(ValueError, match='binding fence'):
        create_app('testing')
