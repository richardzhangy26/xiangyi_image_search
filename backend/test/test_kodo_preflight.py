"""Kodo S3 只读来源适配器与迁移 preflight 的行为测试。"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest
from PIL import Image

from scripts.migrate_kodo_to_oss import main
from services.kodo_source import (
    KodoConfig,
    KodoS3Source,
    PreflightError,
    run_preflight,
)


def _png_bytes(color: str = "red") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 6), color).save(output, format="PNG")
    return output.getvalue()


class _StreamingBody:
    def __init__(self, content: bytes):
        self._content = content

    def iter_chunks(self, chunk_size: int):
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset : offset + chunk_size]

    def close(self):
        return None


class FakeReadOnlyS3Client:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.calls: list[tuple[str, dict]] = []

    def list_buckets(self):
        self.calls.append(("list_buckets", {}))
        return {
            "Buckets": [
                {
                    "Name": "xiangxipackage",
                    "CreationDate": datetime(2026, 1, 1, tzinfo=timezone.utc),
                }
            ]
        }

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list_objects_v2", kwargs))
        keys = list(self.objects)
        if "ContinuationToken" not in kwargs:
            key = keys[0]
            return {
                "Contents": [{"Key": key, "Size": len(self.objects[key])}],
                "IsTruncated": len(keys) > 1,
                "NextContinuationToken": "下一页" if len(keys) > 1 else None,
            }
        return {
            "Contents": [
                {"Key": key, "Size": len(self.objects[key])} for key in keys[1:]
            ],
            "IsTruncated": False,
        }

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        content = self.objects[kwargs["Key"]]
        return {
            "ContentLength": len(content),
            "ContentType": "image/png",
            "ETag": '"fake-etag"',
        }

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        return {"Body": _StreamingBody(self.objects[kwargs["Key"]])}


def _canonical_env() -> dict[str, str]:
    return {
        "QINIU_ACCESS_KEY": "test-access-key",
        "QINIU_SECRET_KEY": "test-secret-key",
        "QINIU_BUCKET_NAME": "xiangxipackage",
        "QINIU_REGION": "z0",
    }


def test_z0_config_resolves_kodo_s3_location():
    config = KodoConfig.from_env(_canonical_env())

    assert config.bucket_name == "xiangxipackage"
    assert config.s3_region == "cn-east-1"
    assert config.endpoint_url == "https://s3.cn-east-1.qiniucs.com"
    assert config.aliases_used == ()


def test_legacy_environment_aliases_are_recorded_without_values(caplog):
    legacy_env = {
        "AccessKey": "legacy-access-value",
        "SecretKey": "legacy-secret-value",
        "BUCKET_NAME": "xiangxipackage",
        "QINIU_REGION": "z0",
    }

    config = KodoConfig.from_env(legacy_env)

    assert config.aliases_used == ("AccessKey", "SecretKey", "BUCKET_NAME")
    assert "AccessKey" in caplog.text
    assert "SecretKey" in caplog.text
    assert "BUCKET_NAME" in caplog.text
    assert "legacy-access-value" not in caplog.text
    assert "legacy-secret-value" not in caplog.text


def test_source_paginates_and_preserves_unicode_space_and_nested_keys():
    objects = {
        "2025.4.18 海报照片/子目录/主图 一.png": _png_bytes(),
        "nested/second.png": _png_bytes("blue"),
    }
    client = FakeReadOnlyS3Client(objects)
    source = KodoS3Source(KodoConfig.from_env(_canonical_env()), client=client)

    listed = list(source.iter_objects())

    assert [item.key for item in listed] == list(objects)
    second_call = [call for call in client.calls if call[0] == "list_objects_v2"][1]
    assert second_call[1]["ContinuationToken"] == "下一页"


def test_preflight_heads_downloads_counts_and_decodes_a_small_image():
    image_key = "2025.4.18 海报照片/子目录/主图 一.png"
    image = _png_bytes()
    objects = {
        "notes/readme.txt": b"x",
        image_key: image,
        "larger/图片.png": _png_bytes("blue") + b"padding",
    }
    client = FakeReadOnlyS3Client(objects)
    source = KodoS3Source(KodoConfig.from_env(_canonical_env()), client=client)

    report = run_preflight(source)

    assert report.source_bucket == "xiangxipackage"
    assert report.s3_region == "cn-east-1"
    assert report.endpoint_url == "https://s3.cn-east-1.qiniucs.com"
    assert report.total_objects == 3
    assert report.image_objects == 2
    assert report.sample.key == image_key
    assert report.sample.head_size == len(image)
    assert report.sample.downloaded_size == len(image)
    assert report.sample.etag == '"fake-etag"'
    assert report.sample.width == 8
    assert report.sample.height == 6
    assert report.sample.image_format == "PNG"
    assert {name for name, _ in client.calls} <= {
        "list_buckets",
        "list_objects_v2",
        "head_object",
        "get_object",
    }


def test_corrupt_image_fails_at_decode_stage():
    client = FakeReadOnlyS3Client({"损坏 图片.png": b"not-an-image"})
    source = KodoS3Source(KodoConfig.from_env(_canonical_env()), client=client)

    with pytest.raises(PreflightError) as exc_info:
        run_preflight(source)

    assert exc_info.value.stage == "decode_image"
    assert exc_info.value.object_key == "损坏 图片.png"


class FailingSource:
    def resolve_location(self):
        raise RuntimeError(
            "AccessDenied access=test-access-key secret=test-secret-key "
            "https://example.test/?X-Amz-Signature=full-signature"
        )


def test_auth_failure_is_nonzero_stage_specific_and_redacted():
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["--preflight"],
        environ=_canonical_env(),
        source_factory=lambda _config: FailingSource(),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code != 0
    error = json.loads(stderr.getvalue())
    assert error["status"] == "failed"
    assert error["stage"] == "resolve_bucket"
    combined_output = stdout.getvalue() + stderr.getvalue()
    assert "test-access-key" not in combined_output
    assert "test-secret-key" not in combined_output
    assert "full-signature" not in combined_output


def test_dry_run_uses_only_read_operations():
    client = FakeReadOnlyS3Client({"中文 目录/小图.png": _png_bytes()})
    source = KodoS3Source(KodoConfig.from_env(_canonical_env()), client=client)
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["--dry-run"],
        environ=_canonical_env(),
        source_factory=lambda _config: source,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    output = json.loads(stdout.getvalue())
    assert output["mode"] == "dry-run"
    assert output["read_only"] is True
    assert stderr.getvalue() == ""
    assert {name for name, _ in client.calls} <= {
        "list_buckets",
        "list_objects_v2",
        "head_object",
        "get_object",
    }
