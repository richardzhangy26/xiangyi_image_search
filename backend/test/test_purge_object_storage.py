import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.backup_storage import (
    BackupStorageConflictError,
    BackupStorageError,
)
from services.purge_object_storage import (
    OssPurgeIsolationStorage,
    OssPurgeSourceReader,
    PurgeIsolationStorageConfig,
    PurgeObjectStorageConfigError,
    PurgeSourceStorageConfig,
)


def _environment():
    return {
        "OSS_ACCESS_KEY_ID": "application-access",
        "OSS_BUCKET_NAME": "private-image-assets",
        "BACKUP_OSS_ACCESS_KEY_ID": "backup-access",
        "BACKUP_OSS_BUCKET_NAME": "private-backups",
        "PURGE_SOURCE_OSS_ACCESS_KEY_ID": "source-read-access",
        "PURGE_SOURCE_OSS_ACCESS_KEY_SECRET": "source-read-secret",
        "PURGE_SOURCE_OSS_ENDPOINT": "oss-cn-shanghai.aliyuncs.com",
        "PURGE_SOURCE_OSS_BUCKET_NAME": "private-image-assets",
        "PURGE_RESTORE_OSS_ACCESS_KEY_ID": "restore-write-access",
        "PURGE_RESTORE_OSS_ACCESS_KEY_SECRET": "restore-write-secret",
        "PURGE_RESTORE_OSS_ENDPOINT": "oss-cn-shanghai.aliyuncs.com",
        "PURGE_RESTORE_OSS_BUCKET_NAME": "disposable-restore",
        "PURGE_RESTORE_OSS_BASE_PREFIX": "isolated-restores",
        "PURGE_RESTORE_OSS_SSE": "AES256",
        "PURGE_RESTORE_ISOLATED": "1",
    }


class FakeOssError(RuntimeError):
    def __init__(self, *, status=None):
        super().__init__("fake-secret-response")
        self.status = status


class FakeDownload:
    def __init__(self, data):
        self._stream = io.BytesIO(data)

    def read(self, size=-1):
        return self._stream.read(size)


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.calls = []

    def head_object(self, key):
        if key not in self.objects:
            raise FakeOssError(status=404)
        data, headers = self.objects[key]
        return SimpleNamespace(
            content_length=len(data),
            headers={**headers, "Content-Length": str(len(data))},
        )

    def get_object(self, key):
        return FakeDownload(self.objects[key][0])

    def put_object_from_file(self, key, path, headers):
        self.objects[key] = (Path(path).read_bytes(), dict(headers))
        self.calls.append((key, dict(headers)))


class FailingBucket:
    def __init__(self, status):
        self.status = status

    def head_object(self, key):
        raise FakeOssError(status=self.status)

    def put_object_from_file(self, key, path, headers):
        raise FakeOssError(status=self.status)


def test_source_config_never_falls_back_to_application_oss_credentials():
    environment = _environment()
    for name in tuple(environment):
        if name.startswith("PURGE_SOURCE_OSS_"):
            environment.pop(name)

    with pytest.raises(PurgeObjectStorageConfigError) as caught:
        PurgeSourceStorageConfig.from_env(environment)

    assert "PURGE_SOURCE_OSS_ACCESS_KEY_ID" in str(caught.value)


def test_source_reader_is_head_get_only():
    config = PurgeSourceStorageConfig.from_env(_environment())
    bucket = FakeBucket()
    bucket.objects["formal/a.png"] = (
        b"source",
        {"x-oss-meta-sha256": "abc"},
    )
    reader = OssPurgeSourceReader(bucket, config)
    target = io.BytesIO()

    found = reader.head("formal/a.png")
    reader.download_to("formal/a.png", target)

    assert found.size == len(b"source")
    assert found.metadata == {"sha256": "abc"}
    assert target.getvalue() == b"source"
    assert not hasattr(reader, "put_file_if_absent")
    assert not hasattr(reader, "delete_object")
    assert not hasattr(reader, "sign_url")


def test_isolation_writer_forces_private_sse_and_forbid_overwrite(tmp_path):
    config = PurgeIsolationStorageConfig.from_env(_environment())
    bucket = FakeBucket()
    writer = OssPurgeIsolationStorage(bucket, config)
    source = tmp_path / "payload"
    source.write_bytes(b"restored")

    writer.put_file_if_absent(
        "isolated-restores/drill-1/purge-batch-1/objects/id",
        source,
        metadata={"sha256": "abc"},
    )

    headers = bucket.calls[0][1]
    assert headers["x-oss-forbid-overwrite"] == "true"
    assert headers["x-oss-object-acl"] == "private"
    assert headers["x-oss-server-side-encryption"] == "AES256"
    assert headers["x-oss-meta-sha256"] == "abc"
    assert not hasattr(writer, "put_bytes_if_absent")
    assert not hasattr(writer, "delete_object")


@pytest.mark.parametrize(
    ("name", "other"),
    [
        ("PURGE_SOURCE_OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_ID"),
        ("PURGE_SOURCE_OSS_ACCESS_KEY_ID", "BACKUP_OSS_ACCESS_KEY_ID"),
        ("PURGE_RESTORE_OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_ID"),
        ("PURGE_RESTORE_OSS_ACCESS_KEY_ID", "BACKUP_OSS_ACCESS_KEY_ID"),
        ("PURGE_RESTORE_OSS_ACCESS_KEY_ID", "PURGE_SOURCE_OSS_ACCESS_KEY_ID"),
    ],
)
def test_role_credentials_must_be_distinct(name, other):
    environment = _environment()
    environment[name] = environment[other]

    with pytest.raises(PurgeObjectStorageConfigError):
        PurgeSourceStorageConfig.from_env(environment)
        PurgeIsolationStorageConfig.from_env(environment)


def test_role_adapters_map_conflicts_and_redact_sdk_error_text(tmp_path):
    source = OssPurgeSourceReader(
        FailingBucket(500),
        PurgeSourceStorageConfig.from_env(_environment()),
    )
    with pytest.raises(BackupStorageError) as source_error:
        source.head("formal/a.png")
    assert "fake-secret-response" not in str(source_error.value)

    isolation = OssPurgeIsolationStorage(
        FailingBucket(409),
        PurgeIsolationStorageConfig.from_env(_environment()),
    )
    payload = tmp_path / "payload"
    payload.write_bytes(b"bytes")
    with pytest.raises(BackupStorageConflictError) as conflict:
        isolation.put_file_if_absent(
            "isolated-restores/run/object",
            payload,
            metadata={},
        )
    assert "fake-secret-response" not in str(conflict.value)
