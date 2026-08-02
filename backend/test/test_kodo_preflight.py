"""Kodo S3 只读来源适配器与迁移 preflight 的行为测试。"""

from __future__ import annotations

import io
import json
import hashlib
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts.migrate_kodo_to_oss import create_parser, main
from services.kodo_config import KodoConfig
from services import kodo_migration
from services.kodo_migration import (
    FullMigrationAuthorization,
    MigrationError,
    MigrationOptions,
    SelectionVerificationBinding,
    run_migration,
    verify_full_migration_authorization,
)
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


def _image_bytes(
    image_format: str,
    *,
    color: str = 'red',
    size: tuple[int, int] = (8, 6),
) -> bytes:
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, format=image_format)
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


def _write_selection_manifest(tmp_path, source_relative_paths):
    manifest_path = tmp_path / 'selection.json'
    manifest_path.write_text(
        json.dumps(
            {'source_relative_paths': source_relative_paths},
            ensure_ascii=False,
        ),
        encoding='utf-8',
    )
    return manifest_path


def _full_authorization(expected_scan=None) -> FullMigrationAuthorization:
    return FullMigrationAuthorization(
        provider='qiniu-kodo',
        bucket='xiangxipackage',
        s3_bucket='xiangxipackage',
        prefix='',
        issue_9_url='https://github.com/richardzhangy26/xiangyi_image_search/issues/9',
        issue_10_evidence_url='https://github.com/richardzhangy26/xiangyi_image_search/issues/10#issuecomment-1',
        user_approval_url='https://github.com/richardzhangy26/xiangyi_image_search/issues/10#issuecomment-2',
        database_backup_reference='test-backup',
        expected_scan=expected_scan or {
            'objects': 1,
            'images': 1,
            'non_images': 0,
            'bytes': 1,
        },
        preflight_generated_at=datetime(2026, 8, 2, 10, tzinfo=timezone.utc),
        dry_run_generated_at=datetime(2026, 8, 2, 10, tzinfo=timezone.utc),
    )


def _write_full_authorization(tmp_path):
    source = {
        'provider': 'qiniu-kodo',
        'bucket': 'xiangxipackage',
        's3_bucket': 'xiangxipackage',
        'prefix': '',
    }
    reports = {}
    for mode in ('preflight', 'dry-run'):
        report_path = tmp_path / f'{mode}.json'
        report = {
            'status': 'ok',
            'generated_at': '2026-08-02T10:00:00+08:00',
            'mode': mode,
            'read_only': True,
            'source': source,
        }
        if mode == 'preflight':
            report.update({
                'total_objects': 1,
                'image_objects': 1,
                'total_bytes': 1,
            })
        else:
            report.update({
                'summary': {
                    'scan': {
                        'objects': 1,
                        'images': 1,
                        'non_images': 0,
                        'bytes': 1,
                    },
                    'selection': {'images': 1, 'bytes': 1},
                },
                'options': {
                    'limit': None,
                    'selection_manifest': False,
                },
                'retry': {'enabled': False},
                'items': [{}],
            })
        report_path.write_text(
            json.dumps(report),
            encoding='utf-8',
        )
        reports[mode] = {
            'path': report_path.name,
            'sha256': hashlib.sha256(report_path.read_bytes()).hexdigest(),
        }
    authorization_path = tmp_path / 'full-authorization.json'
    authorization_path.write_text(
        json.dumps({
            'issue_9_url': 'https://github.com/richardzhangy26/xiangyi_image_search/issues/9',
            'issue_10_evidence_url': 'https://github.com/richardzhangy26/xiangyi_image_search/issues/10#issuecomment-1',
            'user_approval_url': 'https://github.com/richardzhangy26/xiangyi_image_search/issues/10#issuecomment-2',
            'database_backup_reference': 'test-backup',
            'preflight_report': reports['preflight'],
            'dry_run_report': reports['dry-run'],
        }),
        encoding='utf-8',
    )
    return authorization_path


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


def test_dry_run_selects_manifest_paths_in_declared_order_without_writers(
    tmp_path,
):
    source = KodoS3Source(
        KodoConfig.from_env(_canonical_env()),
        client=FakeReadOnlyS3Client({
            '二/图片.png': _png_bytes('blue'),
            '一/图片.png': _png_bytes('red'),
            '说明/readme.txt': b'not an image',
        }),
    )
    manifest_path = _write_selection_manifest(
        tmp_path,
        ['一/图片.png', '二/图片.png'],
    )
    report_path = tmp_path / 'report.json'
    stdout = io.StringIO()

    def forbidden_factory(_environment):
        pytest.fail('清单 dry-run 不得构造 OSS 或 embedding 写端')

    exit_code = main(
        [
            '--dry-run',
            '--selection-manifest',
            str(manifest_path),
            '--report-path',
            str(report_path),
        ],
        environ=_canonical_env(),
        source_factory=lambda _config: source,
        storage_factory=forbidden_factory,
        embedding_factory=forbidden_factory,
        stdout=stdout,
        stderr=io.StringIO(),
    )

    report = json.loads(report_path.read_text(encoding='utf-8'))
    assert exit_code == 0
    assert report['summary']['selection']['images'] == 2
    assert [
        item['source_relative_path']
        for item in report['items']
    ] == ['一/图片.png', '二/图片.png']


@pytest.mark.parametrize(
    'payload',
    [
        {},
        {'source_relative_paths': []},
        {'source_relative_paths': ['一/图片.png', '一/图片.png']},
        {'source_relative_paths': ['一/图片.png', 3]},
    ],
)
def test_invalid_selection_manifest_is_rejected_before_creating_source(
    tmp_path,
    payload,
):
    manifest_path = tmp_path / 'selection.json'
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding='utf-8',
    )
    stderr = io.StringIO()

    exit_code = main(
        ['--dry-run', '--selection-manifest', str(manifest_path)],
        environ=_canonical_env(),
        source_factory=lambda _config: pytest.fail(
            '无效清单不得创建来源客户端'
        ),
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 2
    assert json.loads(stderr.getvalue())['stage'] == 'selection_manifest'


def test_selection_manifest_requires_matching_pilot_count(tmp_path):
    manifest_path = _write_selection_manifest(
        tmp_path,
        ['图/一.png', '图/二.png'],
    )
    stderr = io.StringIO()

    exit_code = main(
        ['--pilot', '1', '--selection-manifest', str(manifest_path)],
        environ=_canonical_env(),
        source_factory=lambda _config: pytest.fail(
            '不匹配的清单不得创建来源客户端'
        ),
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 2
    assert json.loads(stderr.getvalue())['stage'] == 'selection_manifest'


def test_pilot_requires_a_successful_selection_verification_before_source(
    tmp_path,
):
    manifest_path = _write_selection_manifest(
        tmp_path,
        [f'样本/图片 {index}.png' for index in range(10)],
    )
    stderr = io.StringIO()

    exit_code = main(
        ['--pilot', '10', '--selection-manifest', str(manifest_path)],
        environ=_canonical_env(),
        source_factory=lambda _config: pytest.fail(
            '未验证的试迁移不得创建来源客户端'
        ),
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 2
    assert json.loads(stderr.getvalue())['stage'] == 'selection_verification'


def test_full_requires_controlled_authorization_before_source_creation():
    stderr = io.StringIO()

    exit_code = main(
        ['--full'],
        environ=_canonical_env(),
        source_factory=lambda _config: pytest.fail(
            '未经授权的全量迁移不得创建来源客户端'
        ),
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 2
    assert json.loads(stderr.getvalue())['stage'] == 'full_authorization'


def test_full_authorization_checks_github_issue_state_evidence_and_approval(
    monkeypatch,
):
    def fake_run(command, **_kwargs):
        endpoint = command[-1]
        payloads = {
            'repos/richardzhangy26/xiangyi_image_search/issues/9': {
                'state': 'closed',
            },
            'repos/richardzhangy26/xiangyi_image_search/issues/10': {
                'state': 'closed',
            },
            'repos/richardzhangy26/xiangyi_image_search/issues/comments/1': {
                'body': 'preflight、dry-run 和 pilot 试迁移证据已附。',
                'user': {'login': 'agent'},
                'issue_url': 'https://api.github.com/repos/richardzhangy26/xiangyi_image_search/issues/10',
            },
            'repos/richardzhangy26/xiangyi_image_search/issues/comments/2': {
                'body': '批准全量迁移。',
                'user': {'login': 'richardzhangy26'},
                'issue_url': 'https://api.github.com/repos/richardzhangy26/xiangyi_image_search/issues/10',
            },
        }
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payloads[endpoint]),
        )

    monkeypatch.setattr(kodo_migration.subprocess, 'run', fake_run)

    verify_full_migration_authorization(_full_authorization())


def test_verify_selection_reports_required_coverage_without_writers(
    tmp_path,
):
    duplicate = _png_bytes('navy')
    objects = {
        '中文 空格/多层/图片.png': _png_bytes('red'),
        '格式/照片.jpg': _image_bytes('JPEG', color='green'),
        '格式/网页.webp': _image_bytes('WEBP', color='blue'),
        '超大/图片.bmp': _image_bytes(
            'BMP',
            color='yellow',
            size=(3000, 2400),
        ),
        '小图/图片.png': _png_bytes('purple'),
        '重复/一.png': duplicate,
        '重复/二.png': duplicate,
        '普通/三.png': _png_bytes('orange'),
        '普通/四.png': _png_bytes('pink'),
        '普通/五.png': _png_bytes('gray'),
    }
    client = FakeReadOnlyS3Client(objects)
    source = KodoS3Source(KodoConfig.from_env(_canonical_env()), client=client)
    manifest_path = _write_selection_manifest(tmp_path, list(objects))
    report_path = tmp_path / 'verification.json'

    def forbidden_factory(_environment):
        pytest.fail('只读选样验证不得构造 OSS 或 embedding 写端')

    exit_code = main(
        [
            '--verify-selection',
            '--selection-manifest',
            str(manifest_path),
            '--report-path',
            str(report_path),
        ],
        environ=_canonical_env(),
        source_factory=lambda _config: source,
        storage_factory=forbidden_factory,
        embedding_factory=forbidden_factory,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    report = json.loads(report_path.read_text(encoding='utf-8'))
    assert exit_code == 0
    assert report['mode'] == 'verify-selection'
    assert report['read_only'] is True
    assert report['verification']['missing'] == []
    duplicate_reports = [
        item
        for item in report['items']
        if 'duplicate_content' in item['coverage_tags']
    ]
    assert [
        item['source_relative_path'] for item in duplicate_reports
    ] == ['重复/一.png', '重复/二.png']
    assert all(
        call[0] in {
            'list_buckets',
            'list_objects_v2',
            'head_object',
            'get_object',
        }
        for call in client.calls
    )


def test_verify_selection_reports_named_coverage_gaps(tmp_path):
    objects = {
        f'flat/image-{index}.png': _image_bytes(
            'PNG',
            color=(index * 20, 0, 0),
        )
        for index in range(10)
    }
    source = KodoS3Source(
        KodoConfig.from_env(_canonical_env()),
        client=FakeReadOnlyS3Client(objects),
    )
    manifest_path = _write_selection_manifest(tmp_path, list(objects))
    report_path = tmp_path / 'verification.json'

    exit_code = main(
        [
            '--verify-selection',
            '--selection-manifest',
            str(manifest_path),
            '--report-path',
            str(report_path),
        ],
        environ=_canonical_env(),
        source_factory=lambda _config: source,
        storage_factory=lambda _environment: pytest.fail(
            '只读选样验证不得构造 OSS'
        ),
        embedding_factory=lambda _environment: pytest.fail(
            '只读选样验证不得构造 embedding'
        ),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    report = json.loads(report_path.read_text(encoding='utf-8'))
    assert exit_code == 1
    assert set(report['verification']['missing']) >= {
        'chinese_space_path',
        'nested_path',
        'jpeg',
        'webp',
        'over_20_mib',
        'duplicate_content',
    }


def test_pilot_revalidates_hashes_before_constructing_writers():
    duplicate = _png_bytes('navy')
    objects = {
        '中文 空格/多层/图片.png': _png_bytes('red'),
        '格式/照片.jpg': _image_bytes('JPEG', color='green'),
        '格式/网页.webp': _image_bytes('WEBP', color='blue'),
        '超大/图片.bmp': _image_bytes(
            'BMP', color='yellow', size=(3000, 2400),
        ),
        '小图/图片.png': _png_bytes('purple'),
        '重复/一.png': duplicate,
        '重复/二.png': duplicate,
        '普通/图片 07.png': _png_bytes('white'),
        '普通/图片 08.png': _png_bytes('black'),
        '普通/图片 09.png': _png_bytes('gray'),
    }
    source = KodoS3Source(
        KodoConfig.from_env(_canonical_env()),
        client=FakeReadOnlyS3Client(objects),
    )
    paths = tuple(objects)
    options = MigrationOptions.build(
        mode='pilot',
        pilot_count=10,
        selection_keys=paths,
        selection_verification=SelectionVerificationBinding(
            provider='qiniu-kodo',
            bucket='xiangxipackage',
            s3_bucket='xiangxipackage',
            prefix='',
            source_relative_paths=paths,
            content_hashes=('0' * 64,) * 10,
        ),
    )

    with pytest.raises(MigrationError, match='selection_verification'):
        run_migration(
            source,
            options=options,
            ingest_service_factory=lambda _source: pytest.fail(
                '哈希不一致时不得构造入库写端'
            ),
        )


def test_verify_selection_requires_manifest_before_creating_source():
    stderr = io.StringIO()

    exit_code = main(
        ['--verify-selection'],
        environ=_canonical_env(),
        source_factory=lambda _config: pytest.fail(
            '未指定清单时不得创建来源客户端'
        ),
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 2
    assert json.loads(stderr.getvalue())['stage'] == 'selection_manifest'


def test_verify_selection_reports_decode_failure_without_raw_error_text(
    tmp_path,
):
    source = KodoS3Source(
        KodoConfig.from_env(_canonical_env()),
        client=FakeReadOnlyS3Client({
            '损坏/图片.png': b'not-an-image secret=must-not-leak',
        }),
    )
    manifest_path = _write_selection_manifest(tmp_path, ['损坏/图片.png'])
    report_path = tmp_path / 'verification.json'

    exit_code = main(
        [
            '--verify-selection',
            '--selection-manifest',
            str(manifest_path),
            '--report-path',
            str(report_path),
        ],
        environ=_canonical_env(),
        source_factory=lambda _config: source,
        storage_factory=lambda _environment: pytest.fail(
            '失败的只读验证不得构造 OSS'
        ),
        embedding_factory=lambda _environment: pytest.fail(
            '失败的只读验证不得构造 embedding'
        ),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    report = json.loads(report_path.read_text(encoding='utf-8'))
    item = report['items'][0]
    assert exit_code == 1
    assert item['status'] == 'failed'
    assert item['error_stage'] == 'verification'
    assert item['error'] == 'verification:UnidentifiedImageError'
    assert 'must-not-leak' not in json.dumps(report, ensure_ascii=False)


def test_full_rejects_selection_manifest_before_creating_source(tmp_path):
    manifest_path = _write_selection_manifest(tmp_path, ['图片/一.png'])
    stderr = io.StringIO()

    exit_code = main(
        ['--full', '--selection-manifest', str(manifest_path)],
        environ=_canonical_env(),
        source_factory=lambda _config: pytest.fail(
            '--full 不得创建清单限定的来源客户端'
        ),
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 2
    assert json.loads(stderr.getvalue())['stage'] == 'selection_manifest'


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
        options=MigrationOptions.build(
            mode="full",
            batch_size=2,
            full_authorization=_full_authorization({
                'objects': 5,
                'images': 5,
                'non_images': 0,
                'bytes': 15,
            }),
        ),
        ingest_service_factory=lambda _source: service,
    )

    assert service.calls == [
        (keys[0:2], 2),
        (keys[2:4], 2),
        (keys[4:5], 2),
    ]
    assert report["summary"]["outcomes"] == {"created": 5}


def test_full_list_failure_stops_before_write_dependencies_and_redacts(tmp_path):
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
        [
            "--full",
            "--full-authorization",
            str(_write_full_authorization(tmp_path)),
        ],
        environ=_canonical_env(),
        source_factory=lambda _config: ListFailingSource(),
        storage_factory=lambda _environment: pytest.fail(
            "列举失败后不得构造 OSS"
        ),
        embedding_factory=lambda _environment: pytest.fail(
            "列举失败后不得构造 embedding"
        ),
        authorization_verifier=lambda _authorization: None,
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
