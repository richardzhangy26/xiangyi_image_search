"""Kodo → OSS → pgvector 迁移 CLI 的最高层公共 seam。"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from models import ImageAsset
from scripts.migrate_kodo_to_oss import main
from services.embedding import EMBEDDING_DIMENSION
from services.object_source import (
    SourceLocation,
    SourceObject,
    SourceObjectHead,
)
from services.object_storage import (
    ObjectStorageTargetInspection,
    StoredObject,
)


def _image_bytes(
    color: str,
    *,
    image_format: str = "PNG",
    size: tuple[int, int] = (12, 8),
) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format=image_format)
    return output.getvalue()


def _png_bytes(color: str) -> bytes:
    return _image_bytes(color)


class FakeKodo:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = dict(objects)
        self.calls: list[tuple[str, str]] = []
        self.downloaded_paths: list[str] = []

    def resolve_location(self):
        self.calls.append(("resolve", ""))
        return SourceLocation(
            source_bucket="xiangxipackage",
            s3_bucket="xiangxipackage",
            s3_region="cn-east-1",
            endpoint_url="https://s3.cn-east-1.qiniucs.com",
        )

    def iter_objects(self, prefix=""):
        self.calls.append(("list", prefix))
        for key, data in self.objects.items():
            if key.startswith(prefix):
                yield SourceObject(key=key, size=len(data))

    def head_object(self, key):
        self.calls.append(("head", key))
        data = self.objects[key]
        return SourceObjectHead(
            key=key,
            size=len(data),
            content_type="image/png",
            etag='"fake"',
        )

    def download_object(self, key, target, *, max_bytes=None):
        self.calls.append(("get", key))
        self.downloaded_paths.append(target.name)
        data = self.objects[key]
        target.write(data)
        return len(data)


@dataclass
class _Object:
    data: bytes
    content_type: str
    metadata: dict[str, str]


class FakeOss:
    def __init__(self):
        self.objects: dict[str, _Object] = {}
        self.put_calls: list[str] = []
        self.inspected_prefixes: list[str] = []

    def inspect_target(self, base_prefix):
        self.inspected_prefixes.append(base_prefix)
        return ObjectStorageTargetInspection(
            bucket_name="private-image-assets",
            location="oss-cn-shanghai",
            acl="private",
            sample_key=None,
            sample_metadata={},
        )

    def head_object(self, key):
        item = self.objects.get(key)
        if item is None:
            return None
        return StoredObject(
            key=key,
            size=len(item.data),
            content_type=item.content_type,
            metadata=item.metadata,
            etag=hashlib.md5(
                item.data,
                usedforsecurity=False,
            ).hexdigest(),
        )

    def put_file(self, key, source_path, *, spec):
        assert key not in self.objects
        data = Path(source_path).read_bytes()
        self.objects[key] = _Object(
            data=data,
            content_type=spec.content_type,
            metadata=dict(spec.metadata),
        )
        self.put_calls.append(key)

    def put_bytes(self, key, data, *, spec):
        assert key not in self.objects
        self.objects[key] = _Object(
            data=data,
            content_type=spec.content_type,
            metadata=dict(spec.metadata),
        )
        self.put_calls.append(key)


class FakeEmbedding:
    def __init__(self):
        self.batch_sizes: list[int] = []

    def embed_normalized_images(self, image_paths, request_id=None):
        self.batch_sizes.append(len(image_paths))
        return [
            np.full(EMBEDDING_DIMENSION, (index + 1) / 100, dtype=np.float32)
            for index, _path in enumerate(image_paths)
        ]

    def embed_normalized_image(self, image_path, request_id=None):
        raise AssertionError("迁移批处理不得退化为服务层单张入口")


def _environment():
    return {
        "QINIU_ACCESS_KEY": "fake-access",
        "QINIU_SECRET_KEY": "fake-secret",
        "QINIU_BUCKET_NAME": "xiangxipackage",
        "QINIU_REGION": "z0",
        "OSS_ENDPOINT": "oss-cn-shanghai.aliyuncs.com",
        "OSS_BUCKET_NAME": "private-image-assets",
        "OSS_IMAGE_BASE_PREFIX": "image-search",
    }


def _run(
    argv,
    *,
    app,
    source,
    storage,
    embedding,
):
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(
        argv,
        environ=_environment(),
        source_factory=lambda _config: source,
        storage_factory=lambda _environment: storage,
        embedding_factory=lambda _environment: embedding,
        app=app,
        stdout=stdout,
        stderr=stderr,
    )
    output = json.loads(stdout.getvalue()) if stdout.getvalue() else None
    error = json.loads(stderr.getvalue()) if stderr.getvalue() else None
    return exit_code, output, error


def test_pilot_limits_images_clamps_batch_and_writes_a_reconciled_report(
    app,
    tmp_path,
):
    objects = {
        "中文 目录/多层/图片 00.png": _png_bytes("red"),
        "不同格式/相机照片.jpg": _image_bytes(
            "green",
            image_format="JPEG",
        ),
        "不同格式/网页素材.webp": _image_bytes(
            "blue",
            image_format="WEBP",
        ),
        "超大原图/超过 20 MiB.bmp": _image_bytes(
            "yellow",
            image_format="BMP",
            size=(3000, 2400),
        ),
        "小图/不应放大.png": _png_bytes("purple"),
        "普通/图片 05.png": _png_bytes("orange"),
        "普通/图片 06.png": _png_bytes("pink"),
        "普通/图片 07.png": _png_bytes("white"),
        "普通/图片 08.png": _png_bytes("black"),
        "普通/图片 09.png": _png_bytes("gray"),
        "pilot 外/图片 10.png": _png_bytes("cyan"),
        "pilot 外/图片 11.png": _png_bytes("brown"),
    }
    objects["中文 目录/说明.txt"] = b"not an image"
    source = FakeKodo(objects)
    storage = FakeOss()
    embedding = FakeEmbedding()
    report_path = tmp_path / "pilot-report.json"

    exit_code, terminal, error = _run(
        [
            "--pilot",
            "10",
            "--batch-size",
            "999",
            "--report-path",
            str(report_path),
        ],
        app=app,
        source=source,
        storage=storage,
        embedding=embedding,
    )

    assert exit_code == 0
    assert error is None
    assert ImageAsset.query.count() == 10
    assert embedding.batch_sizes == [10]
    complete_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert terminal["mode"] == "pilot"
    assert terminal["read_only"] is False
    assert terminal["options"]["batch_size"] == 20
    assert terminal["summary"]["scan"]["objects"] == 13
    assert terminal["summary"]["scan"]["images"] == 12
    assert terminal["summary"]["scan"]["non_images"] == 1
    assert terminal["summary"]["selection"]["images"] == 10
    assert terminal["summary"]["outcomes"] == {"created": 10}
    assert terminal["summary"]["stages"]["database"]["new"] == 10
    assert terminal["complete_report_written"] is True
    assert "items" not in terminal
    assert max(item["source_size"] for item in complete_report["items"]) > (
        20 * 1024 * 1024
    )
    assert complete_report["summary"] == terminal["summary"]
    assert all(not Path(path).exists() for path in source.downloaded_paths)


def test_full_migration_keeps_duplicate_paths_and_rerun_is_idempotent(app):
    duplicate = _png_bytes("navy")
    source = FakeKodo({
        "目录一/同图.png": duplicate,
        "目录二/同图 副本.png": duplicate,
    })
    storage = FakeOss()
    embedding = FakeEmbedding()

    first_code, first_terminal, _error = _run(
        ["--full"],
        app=app,
        source=source,
        storage=storage,
        embedding=embedding,
    )
    first_put_count = len(storage.put_calls)
    first_embedding_calls = list(embedding.batch_sizes)

    second_code, second, _error = _run(
        ["--full"],
        app=app,
        source=source,
        storage=storage,
        embedding=embedding,
    )

    rows = ImageAsset.query.order_by(ImageAsset.source_relative_path).all()
    assert first_code == second_code == 0
    assert len(rows) == 2
    assert rows[0].content_hash == rows[1].content_hash
    assert rows[0].preview_oss_path == rows[1].preview_oss_path
    assert first_terminal["summary"]["outcomes"] == {"created": 2}
    assert first_embedding_calls == [1]
    assert len(storage.put_calls) == first_put_count
    assert embedding.batch_sizes == first_embedding_calls
    assert second["summary"]["outcomes"] == {"existing": 2}
    assert second["summary"]["stages"]["embedding"]["reused"] == 2


def test_full_migration_resolves_source_location_once_across_batches(app):
    source = FakeKodo({
        f"批量/图片 {index:02d}.png": _png_bytes(
            (index, index * 3 % 255, index * 7 % 255)
        )
        for index in range(41)
    })

    exit_code, _report, error = _run(
        ["--full", "--batch-size", "20"],
        app=app,
        source=source,
        storage=FakeOss(),
        embedding=FakeEmbedding(),
    )

    assert exit_code == 0
    assert error is None
    assert [
        call for call in source.calls
        if call[0] == "resolve"
    ] == [("resolve", "")]


def test_full_rejects_wrong_oss_target_before_embedding_or_download(app):
    source = FakeKodo({"图片/一.png": _png_bytes("red")})

    class WrongBucketStorage:
        def __init__(self):
            self.inspected_prefixes = []

        def inspect_target(self, base_prefix):
            self.inspected_prefixes.append(base_prefix)
            return type("Inspection", (), {
                "bucket_name": "another-bucket",
                "location": "oss-cn-shanghai",
                "acl": "private",
                "sample_key": None,
                "sample_metadata": {},
            })()

    class ForbiddenEmbedding:
        def __init__(self):
            raise AssertionError(
                "OSS 目标预检失败后不得构造 embedding"
            )

    storage = WrongBucketStorage()
    exit_code, report, error = _run(
        ["--full"],
        app=app,
        source=source,
        storage=storage,
        embedding=ForbiddenEmbedding,
    )

    assert exit_code == 1
    assert report is None
    assert error["stage"] == "oss_target_preflight"
    assert storage.inspected_prefixes == ["image-search"]
    assert {name for name, _details in source.calls} == {
        "resolve",
        "list",
    }


def test_retry_failed_only_reprocesses_the_failed_source(app, tmp_path):
    bad_key = "损坏/坏图.png"
    good_key = "正常/好图.png"
    source = FakeKodo({
        bad_key: b"not-an-image",
        good_key: _png_bytes("green"),
    })
    storage = FakeOss()
    embedding = FakeEmbedding()
    first_report = tmp_path / "first.json"

    first_code, first_terminal, _error = _run(
        ["--full", "--report-path", str(first_report)],
        app=app,
        source=source,
        storage=storage,
        embedding=embedding,
    )

    assert first_code == 1
    assert ImageAsset.query.count() == 1
    first_complete = json.loads(first_report.read_text(encoding="utf-8"))
    assert first_terminal["summary"]["outcomes"] == {
        "created": 1,
        "failed": 1,
    }
    failed = [
        item
        for item in first_complete["items"]
        if item["status"] == "failed"
    ]
    assert [item["source_relative_path"] for item in failed] == [bad_key]
    assert failed[0]["error_stage"] == "preview"

    source.objects[bad_key] = _png_bytes("red")
    source.calls.clear()
    second_code, second, _error = _run(
        ["--full", "--retry-failed", str(first_report)],
        app=app,
        source=source,
        storage=storage,
        embedding=embedding,
    )

    assert second_code == 0
    assert ImageAsset.query.count() == 2
    assert second["retry"]["requested"] == 1
    assert second["summary"]["selection"]["images"] == 1
    assert second["summary"]["outcomes"] == {"created": 1}
    assert [call for call in source.calls if call[0] == "get"] == [
        ("get", bad_key)
    ]


def test_terminal_output_is_summary_while_report_keeps_all_items(app, tmp_path):
    source = FakeKodo({
        f"损坏/坏图 {index}.png": b"not-an-image"
        for index in range(7)
    })
    report_path = tmp_path / "all-failures.json"

    exit_code, terminal, error = _run(
        ["--full", "--report-path", str(report_path)],
        app=app,
        source=source,
        storage=FakeOss(),
        embedding=FakeEmbedding(),
    )

    complete_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert error is None
    assert "items" not in terminal
    assert len(terminal["failure_examples"]) == 5
    assert terminal["failure_examples_omitted"] == 2
    assert len(complete_report["items"]) == 7
    assert {
        item["source_relative_path"]
        for item in complete_report["items"]
    } == set(source.objects)


def test_changed_content_at_same_source_is_reported_without_overwrite(app):
    key = "来源冲突/同一路径.png"
    source = FakeKodo({key: _png_bytes("red")})
    storage = FakeOss()
    embedding = FakeEmbedding()

    first_code, _first, _error = _run(
        ["--full"],
        app=app,
        source=source,
        storage=storage,
        embedding=embedding,
    )
    original_objects = {
        object_key: item.data
        for object_key, item in storage.objects.items()
    }
    original_put_count = len(storage.put_calls)
    original_embedding_calls = list(embedding.batch_sizes)
    original_row = ImageAsset.query.one()
    original_hash = original_row.content_hash

    source.objects[key] = _png_bytes("blue")
    second_code, second, _error = _run(
        ["--full"],
        app=app,
        source=source,
        storage=storage,
        embedding=embedding,
    )

    row = ImageAsset.query.one()
    assert first_code == 0
    assert second_code == 1
    assert second["summary"]["outcomes"] == {"source_conflict": 1}
    assert second["summary"]["stages"]["database"]["conflict"] == 1
    assert row.content_hash == original_hash
    assert {
        object_key: item.data
        for object_key, item in storage.objects.items()
    } == original_objects
    assert len(storage.put_calls) == original_put_count
    assert embedding.batch_sizes == original_embedding_calls
