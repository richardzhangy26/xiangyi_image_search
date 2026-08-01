"""Kodo S3 只读来源适配器与迁移 preflight 的行为测试。"""

from __future__ import annotations

import io
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts.migrate_kodo_to_oss import create_parser, main
from services.kodo_config import KodoConfig
from services.kodo_migration import MigrationOptions, run_migration
from services.kodo_source import DownloadSizeLimitExceeded, KodoS3Source
from services.object_source import SourceLocation, SourceObject
from services.source_preflight import (
    PreflightError,
    is_image_key,
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
            yield self._content[offset:offset + chunk_size]

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
                {"Key": key, "Size": len(self.objects[key])}
                for key in keys[1:]
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
        "OSS_ENDPOINT": "oss-cn-shanghai.aliyuncs.com",
        "OSS_BUCKET_NAME": "private-image-assets",
        "OSS_IMAGE_BASE_PREFIX": "image-search",
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
    second_call = [
        call for call in client.calls if call[0] == "list_objects_v2"
    ][1]
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


def test_decompression_bomb_fails_at_decode_stage(monkeypatch):
    client = FakeReadOnlyS3Client({"超像素 图片.png": _png_bytes()})
    source = KodoS3Source(KodoConfig.from_env(_canonical_env()), client=client)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)

    with pytest.raises(PreflightError) as exc_info:
        run_preflight(source)

    assert exc_info.value.stage == "decode_image"
    assert exc_info.value.object_key == "超像素 图片.png"


class HeadGrewS3Client(FakeReadOnlyS3Client):
    def list_objects_v2(self, **kwargs):
        response = super().list_objects_v2(**kwargs)
        for item in response.get("Contents", []):
            item["Size"] = 5
        return response

    def head_object(self, **kwargs):
        response = super().head_object(**kwargs)
        response["ContentLength"] = 11
        return response


def test_head_growth_over_sample_limit_stops_before_get_object():
    client = HeadGrewS3Client({"变化中的图片.png": _png_bytes()})
    source = KodoS3Source(KodoConfig.from_env(_canonical_env()), client=client)

    with pytest.raises(PreflightError) as exc_info:
        run_preflight(source, max_sample_bytes=10)

    assert exc_info.value.stage == "head_object"
    assert "get_object" not in {name for name, _ in client.calls}


def test_stream_download_stops_before_writing_over_limit():
    client = FakeReadOnlyS3Client({"大图.png": _png_bytes()})
    source = KodoS3Source(KodoConfig.from_env(_canonical_env()), client=client)
    source.resolve_location()
    target = io.BytesIO()

    with pytest.raises(DownloadSizeLimitExceeded):
        source.download_object("大图.png", target, max_bytes=10)

    assert target.tell() <= 10


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


def test_public_image_key_classifier_is_case_insensitive():
    assert is_image_key("中文 目录/主图.WEBP") is True
    assert is_image_key("中文 目录/说明.txt") is False


def test_no_mode_defaults_to_dry_run_without_constructing_write_dependencies():
    client = FakeReadOnlyS3Client({
        "中文 目录/小图.png": _png_bytes(),
        "notes/readme.txt": b"read only",
    })
    source = KodoS3Source(KodoConfig.from_env(_canonical_env()), client=client)
    stdout = io.StringIO()

    def forbidden_factory(_environment):
        raise AssertionError("dry-run 不得构造写端或 embedding")

    exit_code = main(
        [],
        environ=_canonical_env(),
        source_factory=lambda _config: source,
        storage_factory=forbidden_factory,
        embedding_factory=forbidden_factory,
        stdout=stdout,
        stderr=io.StringIO(),
    )

    report = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert report["mode"] == "dry-run"
    assert report["read_only"] is True
    assert report["summary"]["scan"] == {
        "objects": 2,
        "images": 1,
        "non_images": 1,
        "bytes": len(_png_bytes()) + len(b"read only"),
    }
    assert report["summary"]["selection"]["images"] == 1
    assert report["summary"]["outcomes"] == {"planned": 1}
    assert report["failure_examples"] == []
    assert report["failure_examples_omitted"] == 0
    assert report["complete_report_written"] is False
    assert "items" not in report


def test_pilot_and_full_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        create_parser().parse_args(["--pilot", "10", "--full"])


def test_retry_failed_in_default_dry_run_selects_only_failed_items(tmp_path):
    retry_report = tmp_path / "previous.json"
    retry_report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": {
                    "provider": "qiniu-kodo",
                    "bucket": "xiangxipackage",
                    "s3_bucket": "xiangxipackage",
                    "prefix": "",
                },
                "items": [
                    {
                        "source_relative_path": "失败/坏图.png",
                        "status": "failed",
                    },
                    {
                        "source_relative_path": "成功/好图.png",
                        "status": "created",
                    },
                    {
                        "source_relative_path": "冲突/变化.png",
                        "status": "source_conflict",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    objects = {
        "失败/坏图.png": _png_bytes("red"),
        "成功/好图.png": _png_bytes("green"),
        "冲突/变化.png": _png_bytes("blue"),
    }
    client = FakeReadOnlyS3Client(objects)
    source = KodoS3Source(KodoConfig.from_env(_canonical_env()), client=client)
    stdout = io.StringIO()
    complete_report_path = tmp_path / "retry-dry-run.json"

    exit_code = main(
        [
            "--retry-failed",
            str(retry_report),
            "--report-path",
            str(complete_report_path),
        ],
        environ=_canonical_env(),
        source_factory=lambda _config: source,
        storage_factory=lambda _environment: pytest.fail(
            "默认 dry-run 不得构造 OSS"
        ),
        embedding_factory=lambda _environment: pytest.fail(
            "默认 dry-run 不得构造 embedding"
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    report = json.loads(stdout.getvalue())
    complete_report = json.loads(
        complete_report_path.read_text(encoding="utf-8")
    )
    assert exit_code == 0
    assert report["retry"]["requested"] == 1
    assert report["summary"]["selection"]["images"] == 1
    assert [
        item["source_relative_path"]
        for item in complete_report["items"]
    ] == ["失败/坏图.png"]


@pytest.mark.parametrize(
    ("binding_field", "mismatched_value"),
    (
        ("provider", "other-provider"),
        ("bucket", "another-bucket"),
        ("s3_bucket", "another-s3-bucket"),
        ("prefix", "another-prefix/"),
    ),
)
def test_retry_report_from_another_source_scope_is_rejected_before_listing(
    tmp_path,
    binding_field,
    mismatched_value,
):
    retry_report = tmp_path / f"other-{binding_field}.json"
    source_binding = {
        "provider": "qiniu-kodo",
        "bucket": "xiangxipackage",
        "s3_bucket": "xiangxipackage",
        "prefix": "",
    }
    source_binding[binding_field] = mismatched_value
    retry_report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": source_binding,
                "items": [
                    {
                        "source_relative_path": "失败/同名.png",
                        "status": "failed",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = FakeReadOnlyS3Client({"失败/同名.png": _png_bytes()})
    source = KodoS3Source(KodoConfig.from_env(_canonical_env()), client=client)
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["--retry-failed", str(retry_report)],
        environ=_canonical_env(),
        source_factory=lambda _config: source,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue())["stage"] == "retry_report"
    assert [name for name, _details in client.calls] == ["list_buckets"]


def test_retry_report_without_failed_items_selects_nothing(tmp_path):
    retry_report = tmp_path / "successful.json"
    retry_report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": {
                    "provider": "qiniu-kodo",
                    "bucket": "xiangxipackage",
                    "s3_bucket": "xiangxipackage",
                    "prefix": "",
                },
                "items": [
                    {
                        "source_relative_path": "成功/好图.png",
                        "status": "created",
                    },
                    {
                        "source_relative_path": "冲突/变化.png",
                        "status": "source_conflict",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    objects = {
        "成功/好图.png": _png_bytes("green"),
        "其他/未迁移.png": _png_bytes("blue"),
    }
    source = KodoS3Source(
        KodoConfig.from_env(_canonical_env()),
        client=FakeReadOnlyS3Client(objects),
    )
    stdout = io.StringIO()

    exit_code = main(
        ["--retry-failed", str(retry_report)],
        environ=_canonical_env(),
        source_factory=lambda _config: source,
        stdout=stdout,
        stderr=io.StringIO(),
    )

    report = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert report["retry"] == {
        "enabled": True,
        "requested": 0,
        "matched": 0,
        "missing": [],
    }
    assert report["summary"]["selection"]["images"] == 0
    assert report["summary"]["outcomes"] == {}
    assert report["failure_examples"] == []
    assert report["failure_examples_omitted"] == 0
    assert "items" not in report


def test_limit_applies_to_default_dry_run_selection():
    objects = {
        f"图片/{index}.png": _png_bytes(color)
        for index, color in enumerate(("red", "green", "blue", "black"))
    }
    client = FakeReadOnlyS3Client(objects)
    source = KodoS3Source(KodoConfig.from_env(_canonical_env()), client=client)
    stdout = io.StringIO()

    exit_code = main(
        ["--limit", "2"],
        environ=_canonical_env(),
        source_factory=lambda _config: source,
        stdout=stdout,
        stderr=io.StringIO(),
    )

    report = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert report["summary"]["scan"]["images"] == 4
    assert report["summary"]["selection"]["images"] == 2
    assert report["summary"]["outcomes"] == {"planned": 2}
    assert report["summary"]["stages"] == {
        stage: {
            "new": 0,
            "reused": 0,
            "conflict": 0,
            "failed": 0,
        }
        for stage in (
            "download",
            "original",
            "preview",
            "embedding",
            "database",
        )
    }


def test_batch_size_is_clamped_to_at_least_one_in_report():
    client = FakeReadOnlyS3Client({"图片/一.png": _png_bytes()})
    source = KodoS3Source(KodoConfig.from_env(_canonical_env()), client=client)
    stdout = io.StringIO()

    exit_code = main(
        ["--batch-size", "0"],
        environ=_canonical_env(),
        source_factory=lambda _config: source,
        stdout=stdout,
        stderr=io.StringIO(),
    )

    report = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert report["options"]["batch_size"] == 1


def test_write_runner_chunks_selected_keys_before_calling_ingest_many():
    keys = [f"图片/{index}.png" for index in range(5)]

    class Source:
        def resolve_location(self):
            return SourceLocation(
                source_bucket="xiangxipackage",
                s3_bucket="xiangxipackage",
                s3_region="cn-east-1",
                endpoint_url="https://s3.cn-east-1.qiniucs.com",
            )

        def iter_objects(self, prefix=""):
            return iter(
                SourceObject(key=key, size=index + 1)
                for index, key in enumerate(keys)
            )

    class Service:
        def __init__(self):
            self.calls = []

        def ingest_many(self, batch, *, batch_size):
            self.calls.append((list(batch), batch_size))
            return [
                SimpleNamespace(
                    source_relative_path=key,
                    source_size=1,
                    status="created",
                    stages={"database": "new"},
                )
                for key in batch
            ]

    service = Service()
    report = run_migration(
        Source(),
        options=MigrationOptions.build(mode="full", batch_size=2),
        ingest_service_factory=lambda _source: service,
    )

    assert service.calls == [
        (keys[0:2], 2),
        (keys[2:4], 2),
        (keys[4:5], 2),
    ]
    assert report["summary"]["outcomes"] == {"created": 5}


def test_full_list_failure_stops_before_write_dependencies_and_redacts():
    class ListFailingSource:
        def resolve_location(self):
            return SourceLocation(
                source_bucket="xiangxipackage",
                s3_bucket="xiangxipackage",
                s3_region="cn-east-1",
                endpoint_url="https://s3.cn-east-1.qiniucs.com",
            )

        def iter_objects(self, prefix=""):
            raise RuntimeError(
                "secret=fake-secret "
                "https://example.test/?X-Amz-Signature=full-signature"
            )

    class FakeApp:
        def app_context(self):
            return nullcontext()

    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["--full"],
        environ=_canonical_env(),
        source_factory=lambda _config: ListFailingSource(),
        storage_factory=lambda _environment: pytest.fail(
            "列举失败后不得构造 OSS"
        ),
        embedding_factory=lambda _environment: pytest.fail(
            "列举失败后不得构造 embedding"
        ),
        app=FakeApp(),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    error = json.loads(stderr.getvalue())
    assert error["stage"] == "list_objects"
    assert error["error"] == "RuntimeError"
    assert "fake-secret" not in stderr.getvalue()
    assert "full-signature" not in stderr.getvalue()


def test_legacy_public_url_migration_entry_refuses_to_run():
    from scripts.migrate_oss_path import main as legacy_main

    stderr = io.StringIO()

    exit_code = legacy_main(
        ["--force"],
        stderr=stderr,
    )

    assert exit_code == 2
    assert "已退役" in stderr.getvalue()
    assert "migrate_kodo_to_oss" in stderr.getvalue()
