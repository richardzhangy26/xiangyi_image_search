import importlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest


def _module():
    try:
        return importlib.import_module("services.backup_storage")
    except ModuleNotFoundError:
        pytest.fail("services.backup_storage 尚未实现")


def _environment():
    return {
        "BACKUP_OSS_ACCESS_KEY_ID": "backup-access",
        "BACKUP_OSS_ACCESS_KEY_SECRET": "backup-secret",
        "BACKUP_OSS_ENDPOINT": "oss-cn-shanghai.aliyuncs.com",
        "BACKUP_OSS_BUCKET_NAME": "private-database-backups",
        "BACKUP_OSS_BASE_PREFIX": "postgresql-backups",
        "BACKUP_OSS_SSE": "AES256",
        "OSS_ACCESS_KEY_ID": "application-access",
        "OSS_BUCKET_NAME": "private-image-assets",
    }


class FakeOssError(RuntimeError):
    def __init__(self, message="sdk-secret", *, status=None):
        super().__init__(message)
        self.status = status


class FakeDownload:
    def __init__(self, data):
        self._stream = io.BytesIO(data)

    def read(self, size=-1):
        return self._stream.read(size)


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.put_calls = []
        self.error = None

    def head_object(self, key):
        if self.error:
            raise self.error
        if key not in self.objects:
            raise FakeOssError(status=404)
        data, headers = self.objects[key]
        return SimpleNamespace(
            content_length=len(data),
            headers={
                **headers,
                "Content-Length": str(len(data)),
            },
        )

    def put_object_from_file(self, key, path, headers):
        if self.error:
            raise self.error
        data = Path(path).read_bytes()
        self.objects[key] = (data, dict(headers))
        self.put_calls.append(("file", key, dict(headers)))

    def put_object(self, key, data, headers):
        if self.error:
            raise self.error
        self.objects[key] = (bytes(data), dict(headers))
        self.put_calls.append(("bytes", key, dict(headers)))

    def get_object(self, key):
        if self.error:
            raise self.error
        return FakeDownload(self.objects[key][0])


def test_config_requires_dedicated_bucket_and_credentials():
    module = _module()
    environment = _environment()
    environment["OSS_BUCKET_NAME"] = environment["BACKUP_OSS_BUCKET_NAME"]

    with pytest.raises(module.BackupStorageConfigError, match="必须独立"):
        module.BackupStorageConfig.from_env(environment)

    environment = _environment()
    environment["OSS_ACCESS_KEY_ID"] = environment["BACKUP_OSS_ACCESS_KEY_ID"]
    with pytest.raises(module.BackupStorageConfigError, match="凭证必须独立"):
        module.BackupStorageConfig.from_env(environment)


@pytest.mark.parametrize(
    "role_prefix",
    ["PURGE_SOURCE_OSS", "PURGE_RESTORE_OSS"],
)
def test_backup_config_rejects_purge_role_credential_or_bucket_reuse(
    role_prefix,
):
    module = _module()
    environment = _environment()
    environment[f"{role_prefix}_ACCESS_KEY_ID"] = (
        environment["BACKUP_OSS_ACCESS_KEY_ID"]
    )

    with pytest.raises(module.BackupStorageConfigError, match="凭证必须独立"):
        module.BackupStorageConfig.from_env(environment)

    environment = _environment()
    environment[f"{role_prefix}_BUCKET_NAME"] = (
        environment["BACKUP_OSS_BUCKET_NAME"]
    )
    with pytest.raises(module.BackupStorageConfigError, match="Bucket 必须独立"):
        module.BackupStorageConfig.from_env(environment)


def test_puts_force_private_sse_and_forbid_overwrite(tmp_path):
    module = _module()
    config = module.BackupStorageConfig.from_env(_environment())
    bucket = FakeBucket()
    storage = module.OssBackupStorage(bucket, config)
    source = tmp_path / "backup.dump"
    source.write_bytes(b"archive")

    storage.put_file_if_absent(
        "postgresql-backups/daily-2026-08-06/backup.dump",
        source,
        metadata={"sha256": "abc"},
    )

    headers = bucket.put_calls[0][2]
    assert headers["x-oss-forbid-overwrite"] == "true"
    assert headers["x-oss-object-acl"] == "private"
    assert headers["x-oss-server-side-encryption"] == "AES256"
    assert headers["x-oss-meta-sha256"] == "abc"


def test_head_and_download_round_trip_without_delete_capability():
    module = _module()
    config = module.BackupStorageConfig.from_env(_environment())
    bucket = FakeBucket()
    storage = module.OssBackupStorage(bucket, config)
    key = "postgresql-backups/daily-2026-08-06/manifest.json"
    storage.put_bytes_if_absent(key, b"{}", metadata={"sha256": "def"})

    found = storage.head(key)
    target = io.BytesIO()
    storage.download_to(key, target)

    assert found.key == key
    assert found.size == 2
    assert found.metadata == {"sha256": "def"}
    assert target.getvalue() == b"{}"
    assert not hasattr(storage, "delete_object")


def test_storage_rejects_unsafe_keys_and_redacts_sdk_errors(tmp_path):
    module = _module()
    config = module.BackupStorageConfig.from_env(_environment())
    bucket = FakeBucket()
    storage = module.OssBackupStorage(bucket, config)

    with pytest.raises(module.BackupStorageConfigError, match="对象键"):
        storage.head("../escape")

    bucket.error = FakeOssError("backup-secret", status=500)
    with pytest.raises(module.BackupStorageError) as caught:
        storage.head("postgresql-backups/safe/object")
    assert "backup-secret" not in str(caught.value)


def test_conflict_is_stable_and_does_not_expose_sdk_message():
    module = _module()
    config = module.BackupStorageConfig.from_env(_environment())
    bucket = FakeBucket()
    bucket.error = FakeOssError("secret response", status=409)
    storage = module.OssBackupStorage(bucket, config)

    with pytest.raises(module.BackupStorageConflictError) as caught:
        storage.put_bytes_if_absent(
            "postgresql-backups/safe/manifest.json",
            b"{}",
            metadata={},
        )
    assert str(caught.value) == "备份对象已存在且禁止覆盖"
