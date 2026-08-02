"""Kodo 图片资产迁移的模式、选取、批处理与审计报告。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import warnings
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError

from .embedding import MAX_BATCH_SIZE
from .image_normalizer import ImageNormalizer
from .object_source import ReadOnlyObjectSource, SourceObject
from .source_preflight import is_image_key, safe_exception_summary

REPORT_SCHEMA_VERSION = 1
WRITE_MODES = frozenset({"pilot", "full"})
VERIFY_SELECTION_MODE = "verify-selection"
REQUIRED_SELECTION_COVERAGE = frozenset({
    "chinese_space_path",
    "nested_path",
    "jpeg",
    "png",
    "webp",
    "over_20_mib",
    "duplicate_content",
    "small_source",
})
LARGE_SOURCE_BYTES = 20 * 1024 * 1024
REPORT_STAGES = (
    "download",
    "original",
    "preview",
    "embedding",
    "database",
)
REPORT_STAGE_STATES = (
    "new",
    "reused",
    "conflict",
    "failed",
)


class MigrationError(RuntimeError):
    """迁移无法安全开始或完成的顶层错误。"""

    def __init__(self, stage: str, detail: str):
        self.stage = stage
        self.detail = detail
        super().__init__(f"{stage}: {detail}")

    def to_dict(self) -> dict[str, str]:
        return {
            "status": "failed",
            "stage": self.stage,
            "error": self.detail,
        }


class LocationCachedObjectSource:
    """缓存一次来源位置解析，同时保持其余只读操作原样委托。"""

    def __init__(self, source: ReadOnlyObjectSource):
        self._source = source
        self._location = None

    def resolve_location(self):
        if self._location is None:
            self._location = self._source.resolve_location()
        return self._location

    def iter_objects(self, prefix: str = ""):
        return self._source.iter_objects(prefix)

    def head_object(self, key: str):
        return self._source.head_object(key)

    def download_object(self, key: str, target, *, max_bytes=None):
        return self._source.download_object(
            key,
            target,
            max_bytes=max_bytes,
        )


@dataclass(frozen=True)
class RetryReportBinding:
    """retry 报告必须绑定到原来源与原前缀，避免跨环境误迁移。"""

    provider: str
    bucket: str
    s3_bucket: str
    prefix: str
    failed_keys: tuple[str, ...]


@dataclass(frozen=True)
class MigrationOptions:
    """一次迁移运行的已校验选项。"""

    mode: str = "dry-run"
    prefix: str = ""
    pilot_count: Optional[int] = None
    limit: Optional[int] = None
    batch_size: int = MAX_BATCH_SIZE
    selection_keys: tuple[str, ...] = ()
    retry_enabled: bool = False
    retry_failed_keys: tuple[str, ...] = ()
    retry_binding: Optional[RetryReportBinding] = None

    @classmethod
    def build(
        cls,
        *,
        mode: str,
        prefix: str = "",
        pilot_count: Optional[int] = None,
        limit: Optional[int] = None,
        batch_size: int = MAX_BATCH_SIZE,
        selection_keys: Sequence[str] = (),
        retry_enabled: bool = False,
        retry_failed_keys: Sequence[str] = (),
        retry_binding: Optional[RetryReportBinding] = None,
    ) -> "MigrationOptions":
        if mode not in {"dry-run", VERIFY_SELECTION_MODE, "pilot", "full"}:
            raise ValueError(
                "迁移模式必须是 dry-run、verify-selection、pilot 或 full"
            )
        if mode == "pilot":
            if pilot_count is None or pilot_count <= 0:
                raise ValueError("--pilot 必须大于 0")
        elif pilot_count is not None:
            raise ValueError("只有 pilot 模式可以设置 pilot_count")
        if limit is not None and limit <= 0:
            raise ValueError("--limit 必须大于 0")
        normalized_selection_keys = tuple(selection_keys)
        if normalized_selection_keys:
            if retry_enabled or retry_failed_keys or retry_binding is not None:
                raise ValueError(
                    "--selection-manifest 不能与 --retry-failed 同时使用"
                )
            if mode == "full":
                raise ValueError(
                    "--full 不能使用 --selection-manifest"
                )
            if mode == "pilot" and len(normalized_selection_keys) != pilot_count:
                raise ValueError(
                    "--pilot 数量必须与 --selection-manifest 项数一致"
                )
        elif mode == VERIFY_SELECTION_MODE:
            raise ValueError(
                "--verify-selection 必须提供 --selection-manifest"
            )
        try:
            requested_batch_size = int(batch_size)
        except (TypeError, ValueError) as exc:
            raise ValueError("--batch-size 必须是整数") from exc
        effective_batch_size = max(
            1,
            min(requested_batch_size, MAX_BATCH_SIZE),
        )
        retry_is_enabled = (
            retry_enabled
            or bool(retry_failed_keys)
            or retry_binding is not None
        )
        if retry_is_enabled and retry_binding is None:
            raise ValueError("retry 必须来自带来源绑定的迁移报告")

        return cls(
            mode=mode,
            prefix=prefix,
            pilot_count=pilot_count,
            limit=limit,
            batch_size=effective_batch_size,
            selection_keys=normalized_selection_keys,
            retry_enabled=retry_is_enabled,
            retry_failed_keys=tuple(dict.fromkeys(retry_failed_keys)),
            retry_binding=retry_binding,
        )


def load_selection_manifest(report_path: Path) -> tuple[str, ...]:
    """读取受控试迁移的有序来源路径清单。"""
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(
            "selection_manifest",
            safe_exception_summary(exc),
        ) from exc

    if not isinstance(payload, dict) or set(payload) != {
        "source_relative_paths"
    }:
        raise MigrationError(
            "selection_manifest",
            "清单必须只包含 source_relative_paths 字段",
        )
    source_relative_paths = payload.get("source_relative_paths")
    if (
        not isinstance(source_relative_paths, list)
        or not source_relative_paths
        or any(
            not isinstance(path, str) or not path
            for path in source_relative_paths
        )
        or len(set(source_relative_paths)) != len(source_relative_paths)
    ):
        raise MigrationError(
            "selection_manifest",
            "source_relative_paths 必须是非空且唯一的字符串数组",
        )
    return tuple(source_relative_paths)


def load_retry_report(report_path: Path) -> RetryReportBinding:
    """读取失败 Key 及其来源绑定；缺失绑定的旧报告拒绝自动重试。"""
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(
            "retry_report",
            safe_exception_summary(exc),
        ) from exc

    if not isinstance(payload, dict):
        raise MigrationError("retry_report", "报告根节点必须是 JSON 对象")
    if payload.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise MigrationError("retry_report", "报告 schema_version 不受支持")
    source = payload.get("source")
    if not isinstance(source, dict):
        raise MigrationError("retry_report", "报告缺少 source 来源绑定")

    provider = source.get("provider")
    bucket = source.get("bucket")
    s3_bucket = source.get("s3_bucket")
    prefix = source.get("prefix")
    if (
        not isinstance(provider, str)
        or not provider
        or not isinstance(bucket, str)
        or not bucket
        or not isinstance(s3_bucket, str)
        or not s3_bucket
        or not isinstance(prefix, str)
    ):
        raise MigrationError("retry_report", "报告 source 来源绑定无效")

    items = payload.get("items")
    if not isinstance(items, list):
        raise MigrationError("retry_report", "报告缺少 items 数组")

    failed_keys: list[str] = []
    for item in items:
        if not isinstance(item, dict) or item.get("status") != "failed":
            continue
        key = item.get("source_relative_path")
        if isinstance(key, str) and key:
            failed_keys.append(key)
    return RetryReportBinding(
        provider=provider,
        bucket=bucket,
        s3_bucket=s3_bucket,
        prefix=prefix,
        failed_keys=tuple(dict.fromkeys(failed_keys)),
    )


def run_migration(
    source: ReadOnlyObjectSource,
    *,
    options: MigrationOptions,
    ingest_service_factory: Optional[
        Callable[[ReadOnlyObjectSource], Any]
    ] = None,
    before_write: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """扫描 Kodo，并在显式写模式下调用统一资产批量入库服务。"""
    started_at = time.monotonic()
    cached_source = (
        source
        if isinstance(source, LocationCachedObjectSource)
        else LocationCachedObjectSource(source)
    )
    try:
        location = cached_source.resolve_location()
    except Exception as exc:
        raise MigrationError(
            "resolve_bucket",
            safe_exception_summary(exc),
        ) from exc

    _validate_retry_binding(options, location)

    try:
        objects = list(cached_source.iter_objects(options.prefix))
    except Exception as exc:
        raise MigrationError(
            "list_objects",
            safe_exception_summary(exc),
        ) from exc

    image_objects = [item for item in objects if is_image_key(item.key)]
    selected, missing_retry_keys = _select_objects(
        objects,
        image_objects,
        options,
    )

    verification = None
    if options.mode == VERIFY_SELECTION_MODE:
        item_reports, verification = _verify_selected_images(
            cached_source,
            selected,
        )
    elif options.mode in WRITE_MODES:
        if ingest_service_factory is None:
            raise MigrationError(
                "config",
                "写模式缺少图片资产入库服务",
            )
        try:
            if before_write is not None:
                before_write()
            service = ingest_service_factory(cached_source)
        except MigrationError:
            raise
        except Exception as exc:
            raise MigrationError(
                "write_preflight",
                safe_exception_summary(exc),
            ) from exc
        try:
            results = []
            selected_keys = [item.key for item in selected]
            for offset in range(0, len(selected_keys), options.batch_size):
                batch = selected_keys[offset:offset + options.batch_size]
                results.extend(
                    service.ingest_many(
                        batch,
                        batch_size=options.batch_size,
                    )
                )
        except Exception as exc:
            raise MigrationError(
                "ingest",
                safe_exception_summary(exc),
            ) from exc
        item_reports = _result_reports(selected, results)
    else:
        item_reports = [
            {
                "source_relative_path": item.key,
                "source_size": item.size,
                "status": "planned",
                "stages": {},
                "error_stage": None,
                "error": None,
            }
            for item in selected
        ]

    outcome_counts = Counter(
        item["status"] for item in item_reports
    )
    stage_counts = _stage_counts(item_reports)
    has_issues = any(
        status == "failed" or status.endswith("_conflict")
        for status in outcome_counts
    )
    if verification and verification["missing"]:
        has_issues = True
    elapsed_seconds = round(time.monotonic() - started_at, 3)
    scan_bytes = sum(item.size for item in objects)
    selected_bytes = sum(item.size for item in selected)

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "completed_with_issues" if has_issues else "ok",
        "mode": options.mode,
        "read_only": options.mode not in WRITE_MODES,
        "source": {
            "provider": "qiniu-kodo",
            "bucket": location.source_bucket,
            "s3_bucket": location.s3_bucket,
            "s3_region": location.s3_region,
            "prefix": options.prefix,
        },
        "options": {
            "pilot": options.pilot_count,
            "limit": options.limit,
            "batch_size": options.batch_size,
            "selection_manifest": bool(options.selection_keys),
            "selection_count": len(options.selection_keys),
        },
        "retry": {
            "enabled": options.retry_enabled,
            "requested": len(options.retry_failed_keys),
            "matched": (
                len(options.retry_failed_keys) - len(missing_retry_keys)
                if options.retry_failed_keys
                else 0
            ),
            "missing": list(missing_retry_keys),
        },
        **({"verification": verification} if verification else {}),
        "summary": {
            "scan": {
                "objects": len(objects),
                "images": len(image_objects),
                "non_images": len(objects) - len(image_objects),
                "bytes": scan_bytes,
            },
            "selection": {
                "images": len(selected),
                "bytes": selected_bytes,
            },
            "outcomes": dict(sorted(outcome_counts.items())),
            "stages": stage_counts,
        },
        # 保留 #5 preflight/dry-run 报告的顶层计数，方便已有自动化平滑升级。
        "total_objects": len(objects),
        "image_objects": len(image_objects),
        "non_image_objects": len(objects) - len(image_objects),
        "total_bytes": scan_bytes,
        "elapsed_seconds": elapsed_seconds,
        "items": item_reports,
    }


def _verify_selected_images(
    source: ReadOnlyObjectSource,
    selected: Sequence[SourceObject],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    max_preview_edge = ImageNormalizer.from_env().max_edge
    with tempfile.TemporaryDirectory(prefix="kodo-selection-") as directory:
        temporary_root = Path(directory)
        reports = [
            _verify_selected_image(
                source,
                source_object,
                temporary_root / f"source-{index}",
                max_preview_edge=max_preview_edge,
            )
            for index, source_object in enumerate(selected)
        ]

    hashes = Counter(
        report["content_hash"]
        for report in reports
        if report["status"] == "verified"
    )
    for report in reports:
        content_hash = report.get("content_hash")
        if content_hash and hashes[content_hash] > 1:
            report["coverage_tags"].append("duplicate_content")

    covered = sorted({
        tag for report in reports for tag in report["coverage_tags"]
    })
    return reports, {
        "covered": covered,
        "missing": sorted(REQUIRED_SELECTION_COVERAGE - set(covered)),
    }


def _verify_selected_image(
    source: ReadOnlyObjectSource,
    source_object: SourceObject,
    temporary_path: Path,
    *,
    max_preview_edge: int,
) -> dict[str, Any]:
    try:
        source_head = source.head_object(source_object.key)
        with temporary_path.open("w+b") as target:
            downloaded_size = source.download_object(source_object.key, target)
            actual_size = target.tell()
        if (
            downloaded_size != actual_size
            or source_head.size != actual_size
        ):
            raise ValueError("来源 HEAD、下载返回值与实际字节数不一致")

        content_hash = _hash_file(temporary_path)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(temporary_path) as image:
                image.verify()
            with Image.open(temporary_path) as image:
                image.load()
                image_format = image.format or "UNKNOWN"
                width, height = image.size
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        return _verification_failure(source_object, exc)
    except Exception as exc:
        return _verification_failure(source_object, exc)

    return {
        "source_relative_path": source_object.key,
        "source_size": actual_size,
        "status": "verified",
        "content_hash": content_hash,
        "image_format": image_format.upper(),
        "source_width": width,
        "source_height": height,
        "coverage_tags": _selection_coverage_tags(
            source_object,
            image_format=image_format,
            width=width,
            height=height,
            max_preview_edge=max_preview_edge,
        ),
        "stages": {},
        "error_stage": None,
        "error": None,
    }


def _verification_failure(
    source_object: SourceObject,
    error: BaseException,
) -> dict[str, Any]:
    return {
        "source_relative_path": source_object.key,
        "source_size": source_object.size,
        "status": "failed",
        "content_hash": None,
        "image_format": None,
        "source_width": None,
        "source_height": None,
        "coverage_tags": [],
        "stages": {},
        "error_stage": "verification",
        "error": f"verification:{type(error).__name__}",
    }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        while chunk := source_file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _selection_coverage_tags(
    source_object: SourceObject,
    *,
    image_format: str,
    width: int,
    height: int,
    max_preview_edge: int,
) -> list[str]:
    source_relative_path = source_object.key
    normalized_format = image_format.upper()
    tags: list[str] = []
    if " " in source_relative_path and any(
        "\u4e00" <= character <= "\u9fff"
        for character in source_relative_path
    ):
        tags.append("chinese_space_path")
    if source_relative_path.count("/") >= 2:
        tags.append("nested_path")
    if normalized_format == "JPEG":
        tags.append("jpeg")
    elif normalized_format == "PNG":
        tags.append("png")
    elif normalized_format == "WEBP":
        tags.append("webp")
    if source_object.size > LARGE_SOURCE_BYTES:
        tags.append("over_20_mib")
    if max(width, height) <= max_preview_edge:
        tags.append("small_source")
    return tags


def _validate_retry_binding(options: MigrationOptions, location) -> None:
    binding = options.retry_binding
    if binding is None:
        return
    if (
        binding.provider != "qiniu-kodo"
        or binding.bucket != location.source_bucket
        or binding.s3_bucket != location.s3_bucket
        or binding.prefix != options.prefix
    ):
        raise MigrationError(
            "retry_report",
            "retry 报告的来源 Bucket 或筛选前缀与当前运行不一致",
        )


def validate_oss_write_target(
    storage,
    environment: Mapping[str, str],
) -> str:
    """在构造 embedding 前核对私有 OSS 目标和隔离前缀。"""
    expected_bucket = (environment.get("OSS_BUCKET_NAME") or "").strip()
    endpoint = (environment.get("OSS_ENDPOINT") or "").strip()
    raw_prefix = environment.get(
        "OSS_IMAGE_BASE_PREFIX",
        "image-search",
    )
    normalized_prefix = str(raw_prefix).strip("/")

    if not expected_bucket or not endpoint:
        raise MigrationError(
            "oss_target_preflight",
            "缺少 OSS Bucket 或 Endpoint 配置",
        )
    if (
        not normalized_prefix
        or any(
            segment in {"", ".", ".."}
            for segment in normalized_prefix.split("/")
        )
        or any(ord(character) < 32 for character in normalized_prefix)
    ):
        raise MigrationError(
            "oss_target_preflight",
            "OSS_IMAGE_BASE_PREFIX 不是安全的隔离前缀",
        )

    expected_location = _oss_location_from_endpoint(endpoint)
    inspect_target = getattr(storage, "inspect_target", None)
    if not callable(inspect_target):
        raise MigrationError(
            "oss_target_preflight",
            "对象存储适配器不支持目标预检",
        )
    try:
        inspection = inspect_target(normalized_prefix)
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError(
            "oss_target_preflight",
            safe_exception_summary(exc),
        ) from exc

    if (
        getattr(inspection, "bucket_name", None) != expected_bucket
        or getattr(inspection, "location", None) != expected_location
    ):
        raise MigrationError(
            "oss_target_preflight",
            "OSS Bucket 或地域与配置不一致",
        )
    if str(getattr(inspection, "acl", "")).lower() != "private":
        raise MigrationError(
            "oss_target_preflight",
            "OSS Bucket 必须保持 private ACL",
        )

    sample_key = getattr(inspection, "sample_key", None)
    if sample_key is not None:
        if not str(sample_key).startswith(f"{normalized_prefix}/"):
            raise MigrationError(
                "oss_target_preflight",
                "OSS 前缀样本越过隔离边界",
            )
        metadata = {
            str(name).lower(): str(value)
            for name, value in (
                getattr(inspection, "sample_metadata", {}) or {}
            ).items()
        }
        if (
            "sha256" not in metadata
            or not (
                "source-provider" in metadata
                or "normalization-version" in metadata
            )
        ):
            raise MigrationError(
                "oss_target_preflight",
                "OSS 隔离前缀包含无法识别的既有对象",
            )
    return normalized_prefix


def _oss_location_from_endpoint(endpoint: str) -> str:
    parsed = urlparse(
        endpoint if "://" in endpoint else f"https://{endpoint}"
    )
    host = (parsed.hostname or "").lower()
    first_label = host.split(".", 1)[0]
    if first_label.endswith("-internal"):
        first_label = first_label.removesuffix("-internal")
    if not first_label.startswith("oss-") or first_label == "oss-":
        raise MigrationError(
            "oss_target_preflight",
            "无法从 OSS_ENDPOINT 验证地域",
        )
    return first_label


def write_report_atomic(report_path: Path, report: dict[str, Any]) -> None:
    """原子写入 UTF-8 JSON，避免中断后留下半份 retry 报告。"""
    resolved = report_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=resolved.parent,
            prefix=f".{resolved.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(f"{serialized}\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, resolved)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _select_objects(
    objects: Sequence[SourceObject],
    image_objects: Sequence[SourceObject],
    options: MigrationOptions,
) -> tuple[list[SourceObject], tuple[str, ...]]:
    if options.selection_keys:
        objects_by_key = {item.key: item for item in objects}
        images_by_key = {item.key: item for item in image_objects}
        missing_selection = [
            key for key in options.selection_keys if key not in objects_by_key
        ]
        non_image = [
            key
            for key in options.selection_keys
            if key in objects_by_key and key not in images_by_key
        ]
        if missing_selection:
            raise MigrationError(
                "selection_manifest",
                "清单包含当前筛选范围内不存在的来源路径: "
                + ", ".join(missing_selection[:5]),
            )
        if non_image:
            raise MigrationError(
                "selection_manifest",
                "清单包含不受支持的图片来源路径: "
                + ", ".join(non_image[:5]),
            )
        return [images_by_key[key] for key in options.selection_keys], ()

    if options.retry_enabled:
        by_key = {item.key: item for item in image_objects}
        selected = [
            by_key[key]
            for key in options.retry_failed_keys
            if key in by_key
        ]
        missing = tuple(
            key
            for key in options.retry_failed_keys
            if key not in by_key
        )
    else:
        selected = list(image_objects)
        missing = ()

    if options.pilot_count is not None:
        selected = selected[:options.pilot_count]
    if options.limit is not None:
        selected = selected[:options.limit]
    return selected, missing


def _result_reports(
    selected: Sequence[SourceObject],
    results: Sequence[Any],
) -> list[dict[str, Any]]:
    results_by_key = {
        result.source_relative_path: result
        for result in results
    }
    reports: list[dict[str, Any]] = []
    for source_object in selected:
        result = results_by_key.get(source_object.key)
        if result is None:
            reports.append({
                "source_relative_path": source_object.key,
                "source_size": source_object.size,
                "status": "failed",
                "asset_id": None,
                "content_hash": None,
                "oss_path": None,
                "preview_oss_path": None,
                "stages": {},
                "error_stage": "ingest",
                "error": "ingest:missing_result",
            })
            continue

        error_stage = getattr(result, "error_stage", None)
        status = str(getattr(result, "status", "failed"))
        reports.append({
            "source_relative_path": source_object.key,
            "source_size": (
                getattr(result, "source_size", 0) or source_object.size
            ),
            "status": status,
            "asset_id": getattr(result, "asset_id", None),
            "content_hash": getattr(result, "content_hash", None),
            "oss_path": getattr(result, "oss_path", None),
            "preview_oss_path": getattr(
                result,
                "preview_oss_path",
                None,
            ),
            "stages": dict(getattr(result, "stages", {}) or {}),
            "error_stage": error_stage,
            # 不持久化下游原始异常文本，避免凭证、URL 查询串或临时路径泄漏。
            "error": (
                f"{error_stage or 'ingest'}:{status}"
                if status == "failed" or status.endswith("_conflict")
                else None
            ),
        })
    return reports


def _stage_counts(
    item_reports: Sequence[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    counters: defaultdict[str, Counter] = defaultdict(Counter)
    for item in item_reports:
        for stage, state in item.get("stages", {}).items():
            counters[str(stage)][str(state)] += 1

        status = str(item.get("status", ""))
        error_stage = item.get("error_stage")
        if error_stage:
            if status == "failed":
                counters[str(error_stage)]["failed"] += 1
            elif status.endswith("_conflict"):
                counters[str(error_stage)]["conflict"] += 1

    return {
        stage: {
            state: counters[stage].get(state, 0)
            for state in REPORT_STAGE_STATES
        }
        for stage in REPORT_STAGES
    }
