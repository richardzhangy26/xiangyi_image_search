"""私有图片资产预览接口。"""

import time
import uuid

from app import create_app
from models import ImageAsset, db
from services.object_storage import SignedDownloadUrl


class FakeStorage:
    def __init__(self, fail=False):
        self.fail = fail
        self.signed = []

    def sign_download_url(self, key, expires_seconds, *, cache_control=None):
        if self.fail:
            raise RuntimeError(
                'secret=do-not-log&Signature=do-not-log-signature'
            )
        self.signed.append((key, expires_seconds, cache_control))
        return SignedDownloadUrl(
            url=(
                f'https://private.example/{key}'
                '?Expires=123&Signature=short-lived'
            ),
            expires_at=int(time.time()) + expires_seconds,
        )


def _build_app(storage):
    app = create_app('testing')
    app.config['IMAGE_ASSET_STORAGE'] = storage
    app.config['OSS_SIGNED_URL_TTL_SECONDS'] = 73
    with app.app_context():
        db.create_all()
    return app


def _seed_asset(app):
    asset_id = uuid.uuid4()
    preview_key = 'image-search/previews/preview-v1/aa/' + 'a' * 64 + '.jpg'
    with app.app_context():
        db.session.add(ImageAsset(
            id=asset_id,
            model_number=None,
            source_provider='qiniu-kodo',
            source_bucket='xiangxipackage',
            source_relative_path='中文 目录/图片.png',
            source_revision=1,
            oss_path='image-search/xiangxipackage/中文 目录/图片.png',
            preview_oss_path=preview_key,
            content_hash='a' * 64,
            source_size=123,
            source_mime_type='image/png',
            source_width=10,
            source_height=8,
            vector=[0.1] * 1024,
            embedding_model='tongyi-embedding-vision-plus-2026-03-06',
            embedding_dimension=1024,
            normalization_version='preview-v1',
            status='active',
        ))
        db.session.commit()
    return asset_id, preview_key


def test_private_preview_redirect_is_signed_on_demand(caplog):
    storage = FakeStorage()
    app = _build_app(storage)
    asset_id, preview_key = _seed_asset(app)

    response = app.test_client().get(f'/api/image-assets/{asset_id}/preview')

    assert response.status_code == 302
    assert response.headers['Location'].endswith(
        '?Expires=123&Signature=short-lived'
    )
    assert storage.signed == [
        (preview_key, 73, 'private, max-age=73'),
    ]
    cache_header = response.headers['Cache-Control']
    assert cache_header.startswith('private, max-age=')
    max_age = int(cache_header.rsplit('=', 1)[1])
    assert 0 <= max_age <= 73
    with app.app_context():
        persisted = db.session.get(ImageAsset, asset_id)
        assert persisted.preview_oss_path == preview_key
        assert 'Signature=' not in str(persisted.to_dict())
    assert 'short-lived' not in caplog.text


def test_private_preview_returns_404_for_unknown_asset():
    app = _build_app(FakeStorage())

    response = app.test_client().get(
        f'/api/image-assets/{uuid.uuid4()}/preview'
    )

    assert response.status_code == 404


def test_private_preview_signing_failure_is_redacted(caplog):
    app = _build_app(FakeStorage(fail=True))
    asset_id, _preview_key = _seed_asset(app)

    response = app.test_client().get(f'/api/image-assets/{asset_id}/preview')

    assert response.status_code == 503
    assert response.get_json()['error_code'] == 'PREVIEW_SIGNING_ERROR'
    combined = response.get_data(as_text=True) + caplog.text
    assert 'do-not-log' not in combined
