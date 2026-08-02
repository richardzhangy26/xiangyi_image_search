"""Kodo 图片资产迁移的模式、选取、批处理与审计报告。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import warnings
from base64 import b64decode
from collections import Counter, defaultdict
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, cast
from urllib.parse import urlparse

from PIL import Image

from .embedding import MAX_BATCH_SIZE
from .image_normalizer import DEFAULT_MAX_EDGE
from .object_source import ReadOnlyObjectSource, SourceObject, SourceObjectHead
from .source_preflight import is_image_key, safe_exception_summary

REPORT_SCHEMA_VERSION = 1
GITHUB_REPOSITORY = "richardzhangy26/xiangyi_image_search"
GITHUB_OWNER = "richardzhangy26"
MAX_FULL_AUTHORIZATION_REPORT_AGE = timedelta(hours=24)
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
_POSITIVE_APPROVAL_PATTERN = re.compile(
    r"(?:批准|同意|approve(?:d)?)\s*(?:执行|进行)?\s*(?:全量迁移|full migration)",
    re.IGNORECASE,
)
_NEGATED_APPROVAL_PATTERN = re.compile(
    r"(?:不|未|暂缓|拒绝|不能)[^\n]{0,12}(?:批准|同意|approve(?:d)?)",
    re.IGNORECASE,
)
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


class VerifiedSelectionObjectSource:
    """把已复验的试迁移源图固定为临时快照，消除验证与写入之间的 TOCTOU。"""

    def __init__(
        self,
        source: ReadOnlyObjectSource,
        selected: Sequence[SourceObject],
        snapshot_root: Path,
    ):
        self._source = source
        self._selected_by_key = {item.key: item for item in selected}
        self._snapshot_paths = {
            item.key: snapshot_root / f"source-{index}"
            for index, item in enumerate(selected)
        }

    def resolve_location(self):
        return self._source.resolve_location()

    def iter_objects(self, prefix: str = ""):
        return self._source.iter_objects(prefix)

    def head_object(self, key: str):
        selected = self._selected_by_key.get(key)
        if selected is None:
            return self._source.head_object(key)
        return SourceObjectHead(
            key=key,
            size=self._snapshot_paths[key].stat().st_size,
            etag=selected.etag,
        )

    def download_object(self, key: str, target, *, max_bytes=None):
        snapshot_path = self._snapshot_paths.get(key)
        if snapshot_path is None:
            return self._source.download_object(
                key,
                target,
                max_bytes=max_bytes,
            )
        snapshot_size = snapshot_path.stat().st_size
        if max_bytes is not None and snapshot_size > max_bytes:
            raise ValueError("试迁移快照超过读取上限")
        with snapshot_path.open("rb") as snapshot:
            shutil.copyfileobj(snapshot, target)
        return snapshot_size


@dataclass(frozen=True)
class RetryReportBinding:
    """retry 报告必须绑定到原来源与原前缀，避免跨环境误迁移。"""

    provider: str
    bucket: str
    s3_bucket: str
    prefix: str
    failed_keys: tuple[str, ...]


@dataclass(frozen=True)
class SelectionVerificationBinding:
    """成功只读验证报告对受控试迁移样本的不可变声明。"""

    provider: str
    bucket: str
    s3_bucket: str
    prefix: str
    source_relative_paths: tuple[str, ...]
    content_hashes: tuple[str, ...]


@dataclass(frozen=True)
class FullMigrationAuthorization:
    """全量迁移所需的人工审批与只读检查证据绑定。"""

    provider: str
    bucket: str
    s3_bucket: str
    prefix: str
    issue_9_url: str
    issue_10_evidence_url: str
    user_approval_url: str
    database_backup_reference: str
    expected_scan: Mapping[str, int]
    preflight_generated_at: datetime
    dry_run_generated_at: datetime


@dataclass(frozen=True)
class ReadOnlyReportEvidence:
    source: Mapping[str, str]
    generated_at: datetime
    scan: Mapping[str, int]


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
    selection_verification: Optional[SelectionVerificationBinding] = None
    full_authorization: Optional[FullMigrationAuthorization] = None
    selection_max_edge: int = DEFAULT_MAX_EDGE

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
        selection_verification: Optional[SelectionVerificationBinding] = None,
        full_authorization: Optional[FullMigrationAuthorization] = None,
        selection_max_edge: int = DEFAULT_MAX_EDGE,
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
        validate_selection_options(
            mode=mode,
            selection_keys=normalized_selection_keys,
            retry_enabled=(
                retry_enabled or bool(retry_failed_keys) or retry_binding is not None
            ),
            pilot_count=pilot_count,
            selection_verification=selection_verification,
        )
        if mode == "full" and full_authorization is None:
            raise ValueError("--full 必须提供 --full-authorization")
        if mode != "full" and full_authorization is not None:
            raise ValueError("--full-authorization 仅可用于 --full")
        if selection_max_edge <= 0:
            raise ValueError("图片预览最长边必须大于 0")
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
            selection_verification=selection_verification,
            full_authorization=full_authorization,
            selection_max_edge=selection_max_edge,
        )


def validate_selection_options(
    *,
    mode: str,
    selection_keys: Sequence[str],
    retry_enabled: bool,
    pilot_count: Optional[int],
    selection_verification: Optional[SelectionVerificationBinding],
    require_selection_verification: bool = True,
) -> None:
    """在构造 Kodo 客户端前统一拒绝不安全的样本模式组合。"""
    if selection_keys:
        if retry_enabled:
            raise ValueError("--selection-manifest 不能与 --retry-failed 同时使用")
        if mode not in {"dry-run", VERIFY_SELECTION_MODE, "pilot"}:
            raise ValueError(
                "--selection-manifest 仅可用于 dry-run、verify-selection 或 pilot 模式"
            )
    elif mode in {VERIFY_SELECTION_MODE, "pilot"}:
        raise ValueError(f"--{mode} 必须提供 --selection-manifest")

    if mode == "pilot":
        if pilot_count != 10 or len(selection_keys) != 10:
            raise ValueError("--pilot 仅允许受控的 10 张清单试迁移")
        if require_selection_verification and selection_verification is None:
            raise ValueError("--pilot 必须提供 --verified-selection-report")
        if (
            selection_verification is not None
            and selection_verification.source_relative_paths != tuple(selection_keys)
        ):
            raise ValueError("验证报告与 --selection-manifest 的来源路径或顺序不一致")
    elif selection_verification is not None:
        raise ValueError("--verified-selection-report 仅可用于 --pilot")


def load_selection_manifest(manifest_path: Path) -> tuple[str, ...]:
    """读取受控试迁移的有序来源路径清单。"""
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
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


def load_selection_verification_report(
    report_path: Path,
) -> SelectionVerificationBinding:
    """读取成功的选样验证报告；报告中的路径和哈希将于写入前再次核验。"""
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(
            "selection_verification",
            safe_exception_summary(exc),
        ) from exc

    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != REPORT_SCHEMA_VERSION
        or payload.get("mode") != VERIFY_SELECTION_MODE
        or payload.get("status") != "ok"
        or payload.get("read_only") is not True
    ):
        raise MigrationError(
            "selection_verification",
            "验证报告不是成功的 verify-selection 报告",
        )
    source = _require_source_binding(
        payload.get("source"),
        stage="selection_verification",
    )
    verification = payload.get("verification")
    if (
        not isinstance(verification, dict)
        or verification.get("missing") != []
        or not REQUIRED_SELECTION_COVERAGE.issubset(
            set(verification.get("covered", []))
        )
    ):
        raise MigrationError(
            "selection_verification",
            "验证报告未覆盖所有必需的 #10 样本类别",
        )
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != 10:
        raise MigrationError(
            "selection_verification",
            "验证报告必须包含恰好 10 个已验证样本",
        )

    paths: list[str] = []
    hashes: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise MigrationError("selection_verification", "验证报告项格式无效")
        path = item.get("source_relative_path")
        content_hash = item.get("content_hash")
        if (
            item.get("status") != "verified"
            or not isinstance(path, str)
            or not path
            or not isinstance(content_hash, str)
            or len(content_hash) != 64
            or any(character not in "0123456789abcdef" for character in content_hash)
        ):
            raise MigrationError(
                "selection_verification",
                "验证报告包含未验证样本或无效哈希",
            )
        paths.append(path)
        hashes.append(content_hash)
    if len(set(paths)) != len(paths):
        raise MigrationError("selection_verification", "验证报告包含重复来源路径")

    return SelectionVerificationBinding(
        **source,
        source_relative_paths=tuple(paths),
        content_hashes=tuple(hashes),
    )


def load_full_migration_authorization(
    authorization_path: Path,
) -> FullMigrationAuthorization:
    """读取并校验全量迁移的人工批准、备份和本次只读检查证据。"""
    try:
        payload = json.loads(authorization_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(
            "full_authorization",
            safe_exception_summary(exc),
        ) from exc

    expected_fields = {
        "issue_9_url",
        "issue_10_evidence_url",
        "user_approval_url",
        "database_backup_reference",
        "preflight_report",
        "dry_run_report",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise MigrationError(
            "full_authorization",
            "授权文件字段必须完整且不含未识别字段",
        )
    issue_9_url = _require_github_issue_url(
        payload.get("issue_9_url"),
        issue_number=9,
        require_comment=False,
    )
    issue_10_evidence_url = _require_github_issue_url(
        payload.get("issue_10_evidence_url"),
        issue_number=10,
        require_comment=True,
    )
    user_approval_url = _require_approval_reference(
        payload.get("user_approval_url"),
    )
    database_backup_reference = payload.get("database_backup_reference")
    if (
        not isinstance(database_backup_reference, str)
        or not database_backup_reference.strip()
    ):
        raise MigrationError(
            "full_authorization",
            "授权文件必须记录数据库备份或恢复点标识",
        )
    preflight_evidence = _load_attested_read_only_report(
        authorization_path,
        payload.get("preflight_report"),
        expected_mode="preflight",
    )
    dry_run_evidence = _load_attested_read_only_report(
        authorization_path,
        payload.get("dry_run_report"),
        expected_mode="dry-run",
    )
    if preflight_evidence.source != dry_run_evidence.source:
        raise MigrationError(
            "full_authorization",
            "preflight 与 dry-run 报告的来源绑定不一致",
        )
    if preflight_evidence.scan != dry_run_evidence.scan:
        raise MigrationError(
            "full_authorization",
            "preflight 与 dry-run 报告的完整扫描统计不一致",
        )
    now = datetime.now().astimezone()
    if (
        preflight_evidence.generated_at > dry_run_evidence.generated_at
        or now - preflight_evidence.generated_at
        > MAX_FULL_AUTHORIZATION_REPORT_AGE
        or now - dry_run_evidence.generated_at
        > MAX_FULL_AUTHORIZATION_REPORT_AGE
        or preflight_evidence.generated_at > now
        or dry_run_evidence.generated_at > now
    ):
        raise MigrationError(
            "full_authorization",
            "preflight 与 dry-run 必须在过去 24 小时内按顺序重新执行",
        )

    return FullMigrationAuthorization(
        **preflight_evidence.source,
        issue_9_url=issue_9_url,
        issue_10_evidence_url=issue_10_evidence_url,
        user_approval_url=user_approval_url,
        database_backup_reference=database_backup_reference.strip(),
        expected_scan=preflight_evidence.scan,
        preflight_generated_at=preflight_evidence.generated_at,
        dry_run_generated_at=dry_run_evidence.generated_at,
    )


def _load_attested_read_only_report(
    authorization_path: Path,
    value: object,
    *,
    expected_mode: str,
) -> ReadOnlyReportEvidence:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise MigrationError(
            "full_authorization",
            f"{expected_mode} 报告引用格式无效",
        )
    raw_path = value.get("path")
    expected_hash = value.get("sha256")
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(character not in "0123456789abcdef" for character in expected_hash)
    ):
        raise MigrationError(
            "full_authorization",
            f"{expected_mode} 报告引用格式无效",
        )
    report_path = Path(raw_path)
    if not report_path.is_absolute():
        report_path = authorization_path.parent / report_path
    try:
        report_bytes = report_path.read_bytes()
        payload = json.loads(report_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(
            "full_authorization",
            safe_exception_summary(exc),
        ) from exc
    if hashlib.sha256(report_bytes).hexdigest() != expected_hash:
        raise MigrationError(
            "full_authorization",
            f"{expected_mode} 报告的 SHA-256 与授权文件不一致",
        )
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ok"
        or payload.get("mode") != expected_mode
        or payload.get("read_only") is not True
    ):
        raise MigrationError(
            "full_authorization",
            f"{expected_mode} 报告不是成功的只读检查结果",
        )
    generated_at = _require_report_timestamp(payload.get("generated_at"))
    source = _require_source_binding(
        payload.get("source"), stage="full_authorization"
    )
    return ReadOnlyReportEvidence(
        source=source,
        generated_at=generated_at,
        scan=_read_only_scan(payload, expected_mode=expected_mode),
    )


def _require_report_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise MigrationError("full_authorization", "只读报告缺少生成时间")
    try:
        generated_at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MigrationError("full_authorization", "只读报告生成时间无效") from exc
    if generated_at.tzinfo is None:
        raise MigrationError("full_authorization", "只读报告生成时间必须包含时区")
    return generated_at


def _read_only_scan(
    payload: Mapping[str, Any],
    *,
    expected_mode: str,
) -> dict[str, int]:
    if expected_mode == "preflight":
        objects = payload.get("total_objects")
        images = payload.get("image_objects")
        total_bytes = payload.get("total_bytes")
        non_images = (
            objects - images
            if isinstance(objects, int) and isinstance(images, int)
            else None
        )
    else:
        summary = payload.get("summary")
        scan = summary.get("scan") if isinstance(summary, dict) else None
        if not isinstance(scan, dict):
            raise MigrationError("full_authorization", "dry-run 报告缺少完整扫描统计")
        assert isinstance(summary, dict)
        objects = scan.get("objects")
        images = scan.get("images")
        non_images = scan.get("non_images")
        total_bytes = scan.get("bytes")
        selection = summary.get("selection")
        items = payload.get("items")
        if (
            not isinstance(selection, dict)
            or selection.get("images") != images
            or selection.get("bytes") != total_bytes
            or not isinstance(items, list)
            or len(items) != images
        ):
            raise MigrationError(
                "full_authorization",
                "dry-run 报告不是完整可审计的盘点结果",
            )
        options = payload.get("options")
        retry = payload.get("retry")
        if (
            not isinstance(options, dict)
            or options.get("limit") is not None
            or options.get("selection_manifest") is not False
            or not isinstance(retry, dict)
            or retry.get("enabled") is not False
        ):
            raise MigrationError(
                "full_authorization",
                "dry-run 报告必须覆盖完整来源范围",
            )
    if (
        not isinstance(objects, int)
        or objects < 0
        or not isinstance(images, int)
        or images < 0
        or not isinstance(non_images, int)
        or non_images < 0
        or not isinstance(total_bytes, int)
        or total_bytes < 0
        or images + non_images != objects
    ):
        raise MigrationError("full_authorization", "只读报告扫描统计无效")
    return {
        "objects": objects,
        "images": images,
        "non_images": non_images,
        "bytes": total_bytes,
    }


def _require_source_binding(value: object, *, stage: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise MigrationError(stage, "报告缺少来源绑定")
    source = {
        "provider": value.get("provider"),
        "bucket": value.get("bucket"),
        "s3_bucket": value.get("s3_bucket"),
        "prefix": value.get("prefix"),
    }
    if (
        not isinstance(source["provider"], str)
        or not source["provider"]
        or not isinstance(source["bucket"], str)
        or not source["bucket"]
        or not isinstance(source["s3_bucket"], str)
        or not source["s3_bucket"]
        or not isinstance(source["prefix"], str)
    ):
        raise MigrationError(stage, "报告来源绑定无效")
    return cast(dict[str, str], source)


def _require_github_issue_url(
    value: object,
    *,
    issue_number: int,
    require_comment: bool,
) -> str:
    if not isinstance(value, str):
        raise MigrationError("full_authorization", "授权文件中的 GitHub Issue URL 无效")
    parsed = urlparse(value)
    expected_path = f"/{GITHUB_REPOSITORY}/issues/{issue_number}"
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.path != expected_path
        or (require_comment and _issue_comment_id(parsed.fragment) is None)
    ):
        raise MigrationError(
            "full_authorization",
            f"URL 必须指向本仓库 Issue #{issue_number}"
            + (" 的评论" if require_comment else ""),
        )
    return value


def _require_approval_reference(value: object) -> str:
    if not isinstance(value, str):
        raise MigrationError("full_authorization", "用户批准引用无效")
    parsed = urlparse(value)
    issue_path = f"/{GITHUB_REPOSITORY}/issues/10"
    is_issue_comment = (
        parsed.scheme == "https"
        and parsed.netloc == "github.com"
        and parsed.path == issue_path
        and _issue_comment_id(parsed.fragment) is not None
    )
    path_parts = parsed.path.split("/")
    is_parent_prd = (
        parsed.scheme == "https"
        and parsed.netloc == "github.com"
        and path_parts[:4] == ["", *GITHUB_REPOSITORY.split("/"), "blob"]
        and len(path_parts) >= 6
    )
    if not (is_issue_comment or is_parent_prd):
        raise MigrationError(
            "full_authorization",
            "用户批准必须是本仓库 Issue #10 评论或父 PRD 的 GitHub 文件链接",
        )
    return value


def verify_full_migration_authorization(
    authorization: FullMigrationAuthorization,
) -> None:
    """通过 GitHub API 验证 #9、#10 证据和仓库所有者的明确批准。"""
    issue_9 = _github_api_json(f"repos/{GITHUB_REPOSITORY}/issues/9")
    if issue_9.get("state") != "closed":
        raise MigrationError("full_authorization", "Issue #9 尚未完成")
    issue_10 = _github_api_json(f"repos/{GITHUB_REPOSITORY}/issues/10")
    if issue_10.get("state") != "closed":
        raise MigrationError("full_authorization", "Issue #10 尚未完成")

    evidence_comment = _github_api_json(
        _issue_comment_endpoint(authorization.issue_10_evidence_url)
    )
    evidence_body = evidence_comment.get("body")
    if (
        not _comment_belongs_to_issue_10(evidence_comment)
        or not isinstance(evidence_body, str)
        or not _has_issue_10_evidence(evidence_body)
    ):
        raise MigrationError(
            "full_authorization",
            "Issue #10 证据评论必须说明 preflight、dry-run 与试迁移验收",
        )

    parsed_approval = urlparse(authorization.user_approval_url)
    approval_comment_id = _issue_comment_id(parsed_approval.fragment)
    if approval_comment_id is not None:
        approval = _github_api_json(
            f"repos/{GITHUB_REPOSITORY}/issues/comments/{approval_comment_id}"
        )
        approval_body = approval.get("body")
        login = (approval.get("user") or {}).get("login")
        if (
            not _comment_belongs_to_issue_10(approval)
            or login != GITHUB_OWNER
            or not isinstance(approval_body, str)
        ):
            raise MigrationError(
                "full_authorization",
                "全量批准必须由仓库所有者在 Issue #10 评论中明确给出",
            )
        approval_text = approval_body
    else:
        approval_text = _github_prd_content(parsed_approval)

    if not _has_explicit_full_approval(approval_text):
        raise MigrationError(
            "full_authorization",
            "用户批准内容未包含明确的全量迁移批准",
        )


def _github_api_json(endpoint: str) -> Mapping[str, Any]:
    try:
        completed = subprocess.run(
            ["gh", "api", endpoint],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise MigrationError(
            "full_authorization",
            safe_exception_summary(exc),
        ) from exc
    if completed.returncode != 0:
        raise MigrationError(
            "full_authorization",
            "无法通过 GitHub API 验证全量授权",
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MigrationError(
            "full_authorization",
            "GitHub API 返回了无效响应",
        ) from exc
    if not isinstance(payload, dict):
        raise MigrationError(
            "full_authorization",
            "GitHub API 返回了无效授权数据",
        )
    return payload


def _issue_comment_id(fragment: str) -> Optional[str]:
    match = re.fullmatch(r"issuecomment-(\d+)", fragment)
    return match.group(1) if match else None


def _issue_comment_endpoint(url: str) -> str:
    comment_id = _issue_comment_id(urlparse(url).fragment)
    if comment_id is None:
        raise MigrationError("full_authorization", "Issue #10 证据链接不是评论")
    return f"repos/{GITHUB_REPOSITORY}/issues/comments/{comment_id}"


def _has_issue_10_evidence(body: str) -> bool:
    lowered = body.lower()
    return "preflight" in lowered and "dry-run" in lowered and (
        "pilot" in lowered or "试迁移" in body
    )


def _comment_belongs_to_issue_10(comment: Mapping[str, Any]) -> bool:
    return comment.get("issue_url") == (
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/issues/10"
    )


def _has_explicit_full_approval(text: str) -> bool:
    return bool(
        _POSITIVE_APPROVAL_PATTERN.search(text)
        and not _NEGATED_APPROVAL_PATTERN.search(text)
    )


def _github_prd_content(parsed_url) -> str:
    path_parts = parsed_url.path.split("/")
    ref = path_parts[4]
    content_path = "/".join(path_parts[5:])
    payload = _github_api_json(
        f"repos/{GITHUB_REPOSITORY}/contents/{content_path}?ref={ref}"
    )
    content = payload.get("content")
    encoding = payload.get("encoding")
    if not isinstance(content, str) or encoding != "base64":
        raise MigrationError("full_authorization", "父 PRD 内容不可读取")
    try:
        return b64decode(content).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise MigrationError("full_authorization", "父 PRD 内容不可读取") from exc


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
    generated_at = datetime.now().astimezone().isoformat()
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
            max_preview_edge=options.selection_max_edge,
        )
    elif options.mode in WRITE_MODES:
        write_context = (
            tempfile.TemporaryDirectory(prefix="kodo-pilot-snapshot-")
            if options.mode == "pilot"
            else nullcontext(None)
        )
        with write_context as temporary_directory:
            ingest_source: ReadOnlyObjectSource = cached_source
            if options.mode == "pilot":
                if temporary_directory is None:
                    raise MigrationError("config", "试迁移快照目录不可用")
                snapshot_root = Path(temporary_directory)
                item_reports, verification = _verify_selected_images(
                    cached_source,
                    selected,
                    max_preview_edge=options.selection_max_edge,
                    snapshot_root=snapshot_root,
                )
                _validate_current_selection_verification(
                    options.selection_verification,
                    location,
                    options.prefix,
                    selected,
                    item_reports,
                    verification,
                )
                ingest_source = VerifiedSelectionObjectSource(
                    cached_source,
                    selected,
                    snapshot_root,
                )
            if options.mode == "full":
                _validate_full_authorization(
                    options.full_authorization,
                    location,
                    options.prefix,
                    current_scan={
                        "objects": len(objects),
                        "images": len(image_objects),
                        "non_images": len(objects) - len(image_objects),
                        "bytes": sum(item.size for item in objects),
                    },
                )
            if ingest_service_factory is None:
                raise MigrationError(
                    "config",
                    "写模式缺少图片资产入库服务",
                )
            try:
                if before_write is not None:
                    before_write()
                service = ingest_service_factory(ingest_source)
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
        "generated_at": generated_at,
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
    *,
    max_preview_edge: int,
    snapshot_root: Optional[Path] = None,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    if snapshot_root is None:
        with tempfile.TemporaryDirectory(prefix="kodo-selection-") as directory:
            reports = _verify_selected_images_at_root(
                source,
                selected,
                temporary_root=Path(directory),
                max_preview_edge=max_preview_edge,
            )
    else:
        reports = _verify_selected_images_at_root(
            source,
            selected,
            temporary_root=snapshot_root,
            max_preview_edge=max_preview_edge,
        )

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


def _verify_selected_images_at_root(
    source: ReadOnlyObjectSource,
    selected: Sequence[SourceObject],
    *,
    temporary_root: Path,
    max_preview_edge: int,
) -> list[dict[str, Any]]:
    temporary_root.mkdir(parents=True, exist_ok=True)
    return [
        _verify_selected_image(
            source,
            source_object,
            temporary_root / f"source-{index}",
            max_preview_edge=max_preview_edge,
        )
        for index, source_object in enumerate(selected)
    ]


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
            source_size=actual_size,
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
    source_size: int,
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
    if source_size > LARGE_SOURCE_BYTES:
        tags.append("over_20_mib")
    if max(width, height) <= max_preview_edge:
        tags.append("small_source")
    return tags


def _validate_current_selection_verification(
    binding: Optional[SelectionVerificationBinding],
    location,
    prefix: str,
    selected: Sequence[SourceObject],
    item_reports: Sequence[Mapping[str, Any]],
    verification: Mapping[str, Sequence[str]],
) -> None:
    """在任何写端构造前确认 Kodo 当前内容仍等于已批准的验证样本。"""
    if binding is None:
        raise MigrationError(
            "selection_verification",
            "试迁移缺少成功的选样验证报告",
        )
    if (
        binding.provider != "qiniu-kodo"
        or binding.bucket != location.source_bucket
        or binding.s3_bucket != location.s3_bucket
        or binding.prefix != prefix
    ):
        raise MigrationError(
            "selection_verification",
            "验证报告的来源绑定与当前 Kodo 来源不一致",
        )
    selected_paths = tuple(item.key for item in selected)
    if binding.source_relative_paths != selected_paths:
        raise MigrationError(
            "selection_verification",
            "验证报告与当前选样清单不一致",
        )
    if verification.get("missing"):
        raise MigrationError(
            "selection_verification",
            "当前来源不再覆盖所有必需的 #10 样本类别",
        )
    current_hashes = tuple(
        item.get("content_hash")
        for item in item_reports
        if item.get("status") == "verified"
    )
    if (
        len(current_hashes) != len(selected)
        or current_hashes != binding.content_hashes
    ):
        raise MigrationError(
            "selection_verification",
            "当前来源内容与成功验证报告不一致",
        )


def _validate_full_authorization(
    authorization: Optional[FullMigrationAuthorization],
    location,
    prefix: str,
    *,
    current_scan: Mapping[str, int],
) -> None:
    if authorization is None:
        raise MigrationError(
            "full_authorization",
            "全量迁移缺少受控授权文件",
        )
    if (
        authorization.provider != "qiniu-kodo"
        or authorization.bucket != location.source_bucket
        or authorization.s3_bucket != location.s3_bucket
        or authorization.prefix != prefix
    ):
        raise MigrationError(
            "full_authorization",
            "授权文件的只读检查来源与当前运行不一致",
        )
    if dict(authorization.expected_scan) != dict(current_scan):
        raise MigrationError(
            "full_authorization",
            "当前 Kodo 完整扫描与授权文件中的只读基线不一致",
        )


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
