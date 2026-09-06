"""私有 OSS 适配器的安全契约。"""

import time
from types import SimpleNamespace

import pytest

from services.object_storage import (
    ObjectSpec,
    ObjectStorageError,
    OssObjectStorage,
)


class FakeBucket:
    def __init__(self):
        self.calls = []
        self.bucket_name = 'private-image-assets'
        self.head_result = SimpleNamespace(
            content_length=123,
            content_type='image/jpeg',
            headers={
                'Content-Length': '123',
                'ETag': '"098f6bcd4621d373cade4e832627b4f6"',
                'X-OSS-Meta-SHA256': 'a' * 64,
            },
        )

    def head_object(self, key):
        self.calls.append(('head', key))
        return self.head_result

    def get_bucket_info(self):
        self.calls.append(('bucket_info',))
        return SimpleNamespace(
            name=self.bucket_name,
            location='oss-cn-shanghai',
            acl=SimpleNamespace(grant='private'),
        )

    def list_objects_v2(self, **kwargs):
        self.calls.append(('list', kwargs))
        return SimpleNamespace(
            object_list=[SimpleNamespace(key='image-search/original/key.jpg')]
        )

    def put_object_from_file(self, key, filename, headers=None):
        self.calls.append(('put_file', key, filename, headers))

    def put_object(self, key, data, headers=None):
        self.calls.append(('put_bytes', key, data, headers))

    def get_object_to_file(self, key, filename):
        self.calls.append(('download', key, filename))
        with open(filename, 'wb') as target:
            target.write(b'private-preview')

    def sign_url(self, method, key, expires, headers=None, params=None,
                 slash_safe=False):
        self.calls.append(('sign', method, key, expires, slash_safe, params))
        # 与 oss2 一致：过期时刻 = int(time.time()) + expires
        expiration = int(time.time()) + expires
        return f'https://private.example/{key}?Expires={expiration}'


def test_head_returns_size_content_type_and_normalized_metadata():
    bucket = FakeBucket()

    result = OssObjectStorage(bucket).head_object('private/key.jpg')

    assert result.size == 123
    assert result.content_type == 'image/jpeg'
    assert result.metadata == {'sha256': 'a' * 64}
    assert result.etag == '"098f6bcd4621d373cade4e832627b4f6"'


def test_inspect_target_reads_bucket_identity_acl_and_prefix_sample():
    bucket = FakeBucket()

    result = OssObjectStorage(bucket).inspect_target('image-search')

    assert result.bucket_name == 'private-image-assets'
    assert result.location == 'oss-cn-shanghai'
    assert result.acl == 'private'
    assert result.sample_key == 'image-search/original/key.jpg'
    assert result.sample_metadata == {'sha256': 'a' * 64}
    assert bucket.calls == [
        ('bucket_info',),
        ('list', {'prefix': 'image-search/', 'max_keys': 1}),
        ('head', 'image-search/original/key.jpg'),
    ]


def test_uploads_forbid_overwrite_and_attach_metadata(tmp_path):
    bucket = FakeBucket()
    storage = OssObjectStorage(bucket)
    source = tmp_path / 'source.png'
    source.write_bytes(b'original')
    original_spec = ObjectSpec(
        size=8,
        content_type='image/png',
        metadata={'sha256': 'b' * 64},
        md5_hex='919c8b643b7133116b02fc0d9bb7df3f',
    )
    preview_spec = ObjectSpec(
        size=7,
        content_type='image/jpeg',
        metadata={'normalization-version': 'preview-v1'},
        md5_hex='5ebeb6065f64f2346dbb00ab789cf001',
    )

    storage.put_file(
        'original/key.png',
        source,
        spec=original_spec,
    )
    storage.put_bytes(
        'preview/key.jpg',
        b'preview',
        spec=preview_spec,
    )

    for call in bucket.calls:
        headers = call[-1]
        assert headers['x-oss-forbid-overwrite'] == 'true'
        assert headers['Content-Type'].startswith('image/')
        assert headers['Content-MD5']
    assert bucket.calls[0][-1]['x-oss-meta-sha256'] == 'b' * 64
    assert (
        bucket.calls[1][-1]['x-oss-meta-normalization-version']
        == 'preview-v1'
    )


def test_private_signing_does_not_change_bucket_acl(monkeypatch):
    bucket = FakeBucket()
    monkeypatch.setattr(time, 'time', lambda: 1_000_000.0)

    signed = OssObjectStorage(bucket).sign_download_url(
        'preview/key.jpg',
        600,
    )

    assert signed.url == 'https://private.example/preview/key.jpg?Expires=1000200'
    assert signed.expires_at == 1_000_200
    assert bucket.calls == [
        ('sign', 'GET', 'preview/key.jpg', 200, True, None),
    ]


def test_signed_url_is_stable_within_aligned_time_window(monkeypatch):
    bucket = FakeBucket()
    storage = OssObjectStorage(bucket)
    monkeypatch.setattr(time, 'time', lambda: 1_000_000.0)
    first = storage.sign_download_url('preview/key.jpg', 600)
    monkeypatch.setattr(time, 'time', lambda: 1_000_100.0)
    second = storage.sign_download_url('preview/key.jpg', 600)

    assert first.url == second.url
    assert first.expires_at == second.expires_at == 1_000_200

    monkeypatch.setattr(time, 'time', lambda: 1_000_200.0)
    third = storage.sign_download_url('preview/key.jpg', 600)
    assert third.url != first.url
    assert third.expires_at == 1_000_800


def test_signed_url_injects_response_cache_control(monkeypatch):
    bucket = FakeBucket()
    monkeypatch.setattr(time, 'time', lambda: 1_000_000.0)

    OssObjectStorage(bucket).sign_download_url(
        'preview/key.jpg',
        600,
        cache_control='private, max-age=600',
    )

    assert bucket.calls[0][-1] == {
        'response-cache-control': 'private, max-age=600',
    }


def test_private_download_writes_only_the_requested_target(tmp_path):
    bucket = FakeBucket()
    target = tmp_path / 'worker-preview.jpg'

    OssObjectStorage(bucket).download_file('preview/key.jpg', target)

    assert target.read_bytes() == b'private-preview'
    assert bucket.calls == [('download', 'preview/key.jpg', str(target))]


def test_private_download_maps_provider_failure_without_raw_message(tmp_path):
    class SecretFailureBucket(FakeBucket):
        def get_object_to_file(self, key, filename):
            raise RuntimeError('secret-signature-and-provider-body')

    with pytest.raises(ObjectStorageError) as captured:
        OssObjectStorage(SecretFailureBucket()).download_file(
            'preview/key.jpg',
            tmp_path / 'worker-preview.jpg',
        )

    assert str(captured.value) == 'OSS 下载失败: RuntimeError'
    assert 'secret-signature' not in str(captured.value)
