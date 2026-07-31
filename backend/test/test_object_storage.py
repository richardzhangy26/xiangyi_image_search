"""私有 OSS 适配器的安全契约。"""

from types import SimpleNamespace

from services.object_storage import OssObjectStorage


class FakeBucket:
    def __init__(self):
        self.calls = []
        self.head_result = SimpleNamespace(
            content_length=123,
            content_type='image/jpeg',
            headers={
                'Content-Length': '123',
                'X-OSS-Meta-SHA256': 'a' * 64,
            },
        )

    def head_object(self, key):
        self.calls.append(('head', key))
        return self.head_result

    def put_object_from_file(self, key, filename, headers=None):
        self.calls.append(('put_file', key, filename, headers))

    def put_object(self, key, data, headers=None):
        self.calls.append(('put_bytes', key, data, headers))

    def sign_url(self, method, key, expires, headers=None, params=None,
                 slash_safe=False):
        self.calls.append(('sign', method, key, expires, slash_safe))
        return 'https://private.example/signed'


def test_head_returns_size_content_type_and_normalized_metadata():
    bucket = FakeBucket()

    result = OssObjectStorage(bucket).head_object('private/key.jpg')

    assert result.size == 123
    assert result.content_type == 'image/jpeg'
    assert result.metadata == {'sha256': 'a' * 64}


def test_uploads_forbid_overwrite_and_attach_metadata(tmp_path):
    bucket = FakeBucket()
    storage = OssObjectStorage(bucket)
    source = tmp_path / 'source.png'
    source.write_bytes(b'original')

    storage.put_file(
        'original/key.png',
        source,
        content_type='image/png',
        metadata={'sha256': 'b' * 64},
    )
    storage.put_bytes(
        'preview/key.jpg',
        b'preview',
        content_type='image/jpeg',
        metadata={'normalization-version': 'preview-v1'},
    )

    for call in bucket.calls:
        headers = call[-1]
        assert headers['x-oss-forbid-overwrite'] == 'true'
        assert headers['Content-Type'].startswith('image/')
    assert bucket.calls[0][-1]['x-oss-meta-sha256'] == 'b' * 64
    assert (
        bucket.calls[1][-1]['x-oss-meta-normalization-version']
        == 'preview-v1'
    )


def test_private_signing_does_not_change_bucket_acl():
    bucket = FakeBucket()

    signed = OssObjectStorage(bucket).sign_download_url(
        'preview/key.jpg',
        600,
    )

    assert signed == 'https://private.example/signed'
    assert bucket.calls == [('sign', 'GET', 'preview/key.jpg', 600, True)]
