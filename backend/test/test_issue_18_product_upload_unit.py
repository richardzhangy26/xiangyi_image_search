"""Issue #18 产品上传结果适配的纯单元测试。"""

from types import SimpleNamespace

import pytest
from flask import Flask

import blueprints.products_v2 as products_module
from blueprints.products_v2 import (
    attach_product_upload_result,
    asset_ingest_conflict_response,
    summarize_product_upload_results,
)
from services.asset_ingest import AssetIngestConflictError, AssetIngestResult


class FakeSession:
    def __init__(self, asset):
        self.asset = asset

    def get(self, _model, asset_id):
        assert asset_id == self.asset.id
        return self.asset


def _result(status):
    return AssetIngestResult(
        status=status,
        asset_id='asset-18',
        content_hash='a' * 64,
        oss_path='original-key',
        preview_oss_path='preview-key',
        source_relative_path='catalog/item.png',
        source_size=42,
        recovery_action=(
            {'type': 'open_recycle_bin', 'asset_id': 'asset-18'}
            if status == 'in_recycle_bin'
            else None
        ),
    )


def test_archived_ingest_result_is_reported_without_automatic_restore(monkeypatch):
    asset = SimpleNamespace(
        id='asset-18',
        model_number='MODEL-18',
        status='archived',
        archived_at='preserve-me',
    )
    before = vars(asset).copy()
    monkeypatch.setattr(
        products_module,
        'db',
        SimpleNamespace(session=FakeSession(asset)),
    )

    item = attach_product_upload_result(_result('in_recycle_bin'), 'MODEL-18')

    assert item == {
        'asset_id': 'asset-18',
        'source_relative_path': 'catalog/item.png',
        'status': 'in_recycle_bin',
        'recovery_action': {
            'type': 'open_recycle_bin',
            'asset_id': 'asset-18',
        },
    }
    assert vars(asset) == before


def test_active_existing_result_can_attach_only_the_explicit_model(monkeypatch):
    asset = SimpleNamespace(
        id='asset-18', model_number=None, status='active', archived_at=None
    )
    monkeypatch.setattr(
        products_module,
        'db',
        SimpleNamespace(session=FakeSession(asset)),
    )

    item = attach_product_upload_result(_result('existing'), 'MODEL-EXPLICIT')

    assert item['status'] == 'existing'
    assert asset.model_number == 'MODEL-EXPLICIT'
    assert asset.status == 'active'


def test_product_write_summary_keeps_all_non_error_outcomes_separate():
    items = [
        {'asset_id': 'new', 'status': 'created'},
        {'asset_id': 'old', 'status': 'existing'},
        {
            'asset_id': 'bin',
            'status': 'in_recycle_bin',
            'recovery_action': {
                'type': 'open_recycle_bin', 'asset_id': 'bin'
            },
        },
    ]

    assert summarize_product_upload_results(items) == {
        'uploaded_images': 1,
        'reused_images': 1,
        'recycle_bin_images': 1,
        'skipped_duplicates': ['old'],
        'image_results': items,
    }


def test_unknown_ingest_status_is_not_silently_attached(monkeypatch):
    asset = SimpleNamespace(
        id='asset-18', model_number=None, status='active', archived_at=None
    )
    monkeypatch.setattr(
        products_module,
        'db',
        SimpleNamespace(session=FakeSession(asset)),
    )

    assert attach_product_upload_result(_result('source_conflict'), 'MODEL-18') is None
    assert asset.model_number is None


def test_source_content_conflict_has_a_dedicated_safe_http_result():
    app = Flask(__name__)
    conflict = AssetIngestConflictError(
        'internal detail must not be returned',
        kind='source_conflict',
        asset_id='asset-existing-18',
        source_relative_path='catalog/item.png',
    )

    with app.app_context():
        response, status = asset_ingest_conflict_response(conflict)

    assert status == 409
    assert response.get_json() == {
        'error': '来源冲突：同一来源身份已存在不同内容，未覆盖现有资产',
        'error_code': 'IMAGE_ASSET_SOURCE_CONFLICT',
        'image_results': [{
            'asset_id': 'asset-existing-18',
            'source_relative_path': 'catalog/item.png',
            'status': 'source_conflict',
        }],
    }
    assert 'internal detail' not in response.get_data(as_text=True)
