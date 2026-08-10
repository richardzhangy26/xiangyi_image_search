"""永久清除前的正式对象备份与引用安全清单。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, Optional, Protocol, Sequence

from .backup_storage import (
    BackupObject,
    BackupStorageConflictError,
    BackupStorageError,
)
from .postgres_backup import (
    BackupManifest,
    BackupRequest,
    validate_manifest_contract,
)


REFERENCE_CATALOG_VERSION = 1
REQUIRED_REFERENCE_SOURCES = frozenset(
    {"image_assets", "image_import_items"}
)
REFERENCE_OWNER_STATES = {
    "image_assets": frozenset({"active", "archived"}),
    # 未来 PostgreSQL Adapter 将各个非终态映射为这一稳定语义状态。
    "image_import_items": frozenset({"unfinished"}),
}
MANIFEST_SCHEMA_VERSION = 1
RETENTION_DAYS = 30


class PurgeObjectBackupError(RuntimeError):
    """稳定、脱敏的对象备份失败。"""

    def __init__(self, message: str, *, stage: str, error_code: str):
        super().__init__(message)
        self.stage = stage
        self.error_code = error_code


class PurgeObjectConfigError(PurgeObjectBackupError):
    def __init__(self, message: str, *, error_code: str = "invalid_config"):
        super().__init__(message, stage="config", error_code=error_code)


class PurgeObjectReferenceError(PurgeObjectBackupError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "reference_snapshot_invalid",
    ):
        super().__init__(message, stage="reference_snapshot", error_code=error_code)


class PurgeObjectIntegrityError(PurgeObjectBackupError):
    def __init__(
        self,
        message: str,
        *,
        stage: str = "integrity",
        error_code: str = "integrity_failed",
    ):
        super().__init__(message, stage=stage, error_code=error_code)


class PurgeObjectConflictError(PurgeObjectIntegrityError):
    def __init__(self, message: str, *, stage: str = "reconcile"):
        super().__init__(
            message,
            stage=stage,
            error_code="backup_object_conflict",
        )


@dataclass(frozen=True)
class PurgeObjectBackupRequest:
    purge_batch_id: str
    asset_ids: tuple[str, ...]


@dataclass(frozen=True)
class PurgeAssetSnapshot:
    asset_id: str
    status: str
    original_key: str
    preview_key: str
    original_size: int
    original_sha256: str
    normalization_version: str


@dataclass(frozen=True)
class ObjectReference:
    source: str
    owner_id: str
    owner_state: str
    kind: str
    formal_key: str


@dataclass(frozen=True)
class ReferenceSourceSlice:
    source: str
    consistency_token: str
    status: str
    truncated: bool
    enumerated_count: int


@dataclass(frozen=True)
class CompleteReferenceSnapshot:
    catalog_version: int
    consistency_token: str
    captured_at: datetime
    targets: tuple[PurgeAssetSnapshot, ...]
    source_slices: tuple[ReferenceSourceSlice, ...]
    references: tuple[ObjectReference, ...]


class RestorePointGate(Protocol):
    def require_verified(self, purge_batch_id: str) -> BackupManifest:
        ...


class ReferenceSnapshotReader(Protocol):
    def capture_for_purge(
        self,
        asset_ids: tuple[str, ...],
    ) -> CompleteReferenceSnapshot:
        ...


class ReadableObjectStorage(Protocol):
    def head(self, key: str) -> Optional[BackupObject]:
        ...

    def download_to(self, key: str, target: BinaryIO) -> None:
        ...


class WriteOnceObjectStorage(ReadableObjectStorage, Protocol):
    def put_file_if_absent(
        self,
        key: str,
        path: Path,
        *,
        metadata: Mapping[str, str],
    ) -> None:
        ...

    def put_bytes_if_absent(
        self,
        key: str,
        data: bytes,
        *,
        metadata: Mapping[str, str],
    ) -> None:
        ...


@dataclass(frozen=True)
class PurgeObjectBackupConfig:
    formal_bucket: str
    backup_bucket: str
    backup_prefix: str
    local_root: Path
    reference_snapshot_max_age_seconds: int = 300

    def __post_init__(self) -> None:
        if not self.formal_bucket or not self.backup_bucket:
            raise PurgeObjectConfigError("正式与备份 Bucket 身份不能为空")
        if self.formal_bucket == self.backup_bucket:
            raise PurgeObjectConfigError("对象备份 Bucket 必须独立于正式图片 Bucket")
        _validate_key(self.backup_prefix)
        if (
            type(self.reference_snapshot_max_age_seconds) is not int
            or not 1 <= self.reference_snapshot_max_age_seconds <= 3600
        ):
            raise PurgeObjectConfigError("实时引用快照最大时效必须为 1 至 3600 秒")


@dataclass(frozen=True)
class PurgeObjectBackupItem:
    object_id: str
    kind: str
    asset_ids: tuple[str, ...]
    formal_bucket: str
    formal_key: str
    backup_bucket: str
    backup_key: str
    size: int
    sha256: str
    selected_reference_count: int
    total_reference_count: int
    remaining_reference_count: int
    reference_set_sha256: str
    verification: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "kind": self.kind,
            "asset_ids": list(self.asset_ids),
            "formal": {
                "bucket": self.formal_bucket,
                "key": self.formal_key,
            },
            "backup": {
                "bucket": self.backup_bucket,
                "key": self.backup_key,
            },
            "size_bytes": self.size,
            "sha256": self.sha256,
            "reference_evidence": {
                "selected_count": self.selected_reference_count,
                "total_count": self.total_reference_count,
                "remaining_count": self.remaining_reference_count,
                "reference_set_sha256": self.reference_set_sha256,
            },
            "verification": dict(self.verification),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PurgeObjectBackupItem":
        _require_exact_keys(
            payload,
            {
                "object_id",
                "kind",
                "asset_ids",
                "formal",
                "backup",
                "size_bytes",
                "sha256",
                "reference_evidence",
                "verification",
            },
        )
        formal = payload["formal"]
        backup = payload["backup"]
        evidence = payload["reference_evidence"]
        verification = payload["verification"]
        _require_exact_keys(formal, {"bucket", "key"})
        _require_exact_keys(backup, {"bucket", "key"})
        _require_exact_keys(
            evidence,
            {
                "selected_count",
                "total_count",
                "remaining_count",
                "reference_set_sha256",
            },
        )
        _require_exact_keys(
            verification,
            {
                "source_head_download",
                "backup_head",
                "backup_download_sha256",
            },
        )
        if (
            not isinstance(payload["object_id"], str)
            or not isinstance(payload["kind"], str)
            or not _is_string_list(payload["asset_ids"])
            or not all(isinstance(value, str) for value in formal.values())
            or not all(isinstance(value, str) for value in backup.values())
            or type(payload["size_bytes"]) is not int
            or not isinstance(payload["sha256"], str)
            or any(type(evidence[name]) is not int for name in (
                "selected_count",
                "total_count",
                "remaining_count",
            ))
            or not isinstance(evidence["reference_set_sha256"], str)
            or not all(
                isinstance(name, str) and isinstance(value, str)
                for name, value in verification.items()
            )
        ):
            raise ValueError("invalid object manifest value types")
        return cls(
            object_id=str(payload["object_id"]),
            kind=str(payload["kind"]),
            asset_ids=tuple(str(value) for value in payload["asset_ids"]),
            formal_bucket=str(formal["bucket"]),
            formal_key=str(formal["key"]),
            backup_bucket=str(backup["bucket"]),
            backup_key=str(backup["key"]),
            size=int(payload["size_bytes"]),
            sha256=str(payload["sha256"]),
            selected_reference_count=int(evidence["selected_count"]),
            total_reference_count=int(evidence["total_count"]),
            remaining_reference_count=int(evidence["remaining_count"]),
            reference_set_sha256=str(evidence["reference_set_sha256"]),
            verification=dict(verification),
        )


@dataclass(frozen=True)
class ReferenceProtectedObject:
    kind: str
    formal_bucket: str
    formal_key: str
    selected_asset_ids: tuple[str, ...]
    selected_reference_count: int
    total_reference_count: int
    remaining_reference_count: int
    reference_set_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "formal": {
                "bucket": self.formal_bucket,
                "key": self.formal_key,
            },
            "selected_asset_ids": list(self.selected_asset_ids),
            "selected_reference_count": self.selected_reference_count,
            "total_reference_count": self.total_reference_count,
            "remaining_reference_count": self.remaining_reference_count,
            "reference_set_sha256": self.reference_set_sha256,
            "action": "reference_protected",
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "ReferenceProtectedObject":
        _require_exact_keys(
            payload,
            {
                "kind",
                "formal",
                "selected_asset_ids",
                "selected_reference_count",
                "total_reference_count",
                "remaining_reference_count",
                "reference_set_sha256",
                "action",
            },
        )
        formal = payload["formal"]
        _require_exact_keys(formal, {"bucket", "key"})
        if payload["action"] != "reference_protected":
            raise ValueError("unknown reference decision")
        if (
            not isinstance(payload["kind"], str)
            or not all(isinstance(value, str) for value in formal.values())
            or not _is_string_list(payload["selected_asset_ids"])
            or any(type(payload[name]) is not int for name in (
                "selected_reference_count",
                "total_reference_count",
                "remaining_reference_count",
            ))
            or not isinstance(payload["reference_set_sha256"], str)
        ):
            raise ValueError("invalid protected-reference value types")
        return cls(
            kind=str(payload["kind"]),
            formal_bucket=str(formal["bucket"]),
            formal_key=str(formal["key"]),
            selected_asset_ids=tuple(
                str(value) for value in payload["selected_asset_ids"]
            ),
            selected_reference_count=int(payload["selected_reference_count"]),
            total_reference_count=int(payload["total_reference_count"]),
            remaining_reference_count=int(payload["remaining_reference_count"]),
            reference_set_sha256=str(payload["reference_set_sha256"]),
        )


@dataclass(frozen=True)
class PurgeObjectBackupManifest:
    schema_version: int
    status: str
    kind: str
    purge_batch_id: str
    database_restore_point: Mapping[str, Any]
    asset_ids: tuple[str, ...]
    reference_catalog_version: int
    reference_snapshot_sha256: str
    plan_key: str
    manifest_key: str
    objects: tuple[PurgeObjectBackupItem, ...]
    reference_protected: tuple[ReferenceProtectedObject, ...]
    retention: Mapping[str, Any]
    created_at: datetime
    completed_at: datetime
    authorization: str
    production_gates: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "kind": self.kind,
            "purge_batch_id": self.purge_batch_id,
            "database_restore_point": dict(self.database_restore_point),
            "selection": {
                "asset_ids": list(self.asset_ids),
                "reference_catalog_version": self.reference_catalog_version,
                "reference_snapshot_sha256": self.reference_snapshot_sha256,
                "reference_protected": [
                    item.to_dict() for item in self.reference_protected
                ],
            },
            "copies": {
                "plan_key": self.plan_key,
                "manifest_key": self.manifest_key,
                "objects": [item.to_dict() for item in self.objects],
            },
            "retention": dict(self.retention),
            "created_at": _iso(self.created_at),
            "completed_at": _iso(self.completed_at),
            "authorization": self.authorization,
            "production_gates": dict(self.production_gates),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "PurgeObjectBackupManifest":
        try:
            _require_exact_keys(
                payload,
                {
                    "schema_version",
                    "status",
                    "kind",
                    "purge_batch_id",
                    "database_restore_point",
                    "selection",
                    "copies",
                    "retention",
                    "created_at",
                    "completed_at",
                    "authorization",
                    "production_gates",
                },
            )
            database_restore_point = payload["database_restore_point"]
            selection = payload["selection"]
            copies = payload["copies"]
            retention = payload["retention"]
            _require_exact_keys(
                database_restore_point,
                {
                    "backup_id",
                    "purge_batch_id",
                    "remote_bucket",
                    "remote_manifest_key",
                    "manifest_sha256",
                    "artifact_sha256",
                    "completed_at",
                    "retain_until",
                },
            )
            _require_exact_keys(
                selection,
                {
                    "asset_ids",
                    "reference_catalog_version",
                    "reference_snapshot_sha256",
                    "reference_protected",
                },
            )
            _require_exact_keys(
                copies,
                {"plan_key", "manifest_key", "objects"},
            )
            _require_exact_keys(retention, {"days", "retain_until"})
            if (
                type(payload["schema_version"]) is not int
                or not all(
                    isinstance(payload[name], str)
                    for name in (
                        "status",
                        "kind",
                        "purge_batch_id",
                        "created_at",
                        "completed_at",
                        "authorization",
                    )
                )
                or not all(
                    isinstance(value, str)
                    for value in database_restore_point.values()
                )
                or not _is_string_list(selection["asset_ids"])
                or type(selection["reference_catalog_version"]) is not int
                or not isinstance(
                    selection["reference_snapshot_sha256"], str
                )
                or not isinstance(selection["reference_protected"], list)
                or not isinstance(copies["plan_key"], str)
                or not isinstance(copies["manifest_key"], str)
                or not isinstance(copies["objects"], list)
                or type(retention["days"]) is not int
                or not isinstance(retention["retain_until"], str)
                or not isinstance(payload["production_gates"], Mapping)
                or not all(
                    isinstance(name, str) and isinstance(value, str)
                    for name, value in payload["production_gates"].items()
                )
            ):
                raise ValueError("invalid manifest value types")
            manifest = cls(
                schema_version=int(payload["schema_version"]),
                status=str(payload["status"]),
                kind=str(payload["kind"]),
                purge_batch_id=str(payload["purge_batch_id"]),
                database_restore_point=dict(database_restore_point),
                asset_ids=tuple(str(value) for value in selection["asset_ids"]),
                reference_catalog_version=int(
                    selection["reference_catalog_version"]
                ),
                reference_snapshot_sha256=str(
                    selection["reference_snapshot_sha256"]
                ),
                plan_key=str(copies["plan_key"]),
                manifest_key=str(copies["manifest_key"]),
                objects=tuple(
                    PurgeObjectBackupItem.from_dict(item)
                    for item in copies["objects"]
                ),
                reference_protected=tuple(
                    ReferenceProtectedObject.from_dict(item)
                    for item in selection["reference_protected"]
                ),
                retention=dict(retention),
                created_at=_parse_datetime(str(payload["created_at"])),
                completed_at=_parse_datetime(str(payload["completed_at"])),
                authorization=str(payload["authorization"]),
                production_gates=dict(payload["production_gates"]),
            )
            _validate_object_manifest(manifest)
            return manifest
        except (KeyError, TypeError, ValueError) as exc:
            raise PurgeObjectIntegrityError(
                "对象备份 manifest schema 或内容无效",
                stage="manifest_validate",
                error_code="invalid_manifest",
            ) from exc


@dataclass(frozen=True)
class VerifiedPurgeObjectBackup:
    status: str
    manifest_key: str
    manifest_sha256: str
    manifest: PurgeObjectBackupManifest

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "status": self.status,
            "manifest_key": self.manifest_key,
            "manifest_sha256": self.manifest_sha256,
            "manifest": self.manifest.to_dict(),
        }


@dataclass(frozen=True)
class CurrentDeletionCandidates:
    status: str
    purge_batch_id: str
    reference_snapshot_sha256: str
    objects: tuple[PurgeObjectBackupItem, ...]

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "status": self.status,
            "purge_batch_id": self.purge_batch_id,
            "reference_snapshot_sha256": self.reference_snapshot_sha256,
            "objects": [item.to_dict() for item in self.objects],
        }


@dataclass(frozen=True)
class _PlannedObject:
    kind: str
    asset_ids: tuple[str, ...]
    formal_key: str
    expected_size: Optional[int]
    expected_sha256: Optional[str]
    normalization_version: Optional[str]
    selected_reference_count: int
    total_reference_count: int
    remaining_reference_count: int
    reference_set_sha256: str


@dataclass(frozen=True)
class _PreparedObject:
    planned: _PlannedObject
    object_id: str
    backup_key: str
    path: Path
    size: int
    sha256: str


class PurgeObjectBackupService:
    """从完整实时引用快照创建经读回校验的对象备份。"""

    def __init__(
        self,
        *,
        restore_points: RestorePointGate,
        references: ReferenceSnapshotReader,
        formal_objects: ReadableObjectStorage,
        backup_store: WriteOnceObjectStorage,
        config: PurgeObjectBackupConfig,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.restore_points = restore_points
        self.references = references
        self.formal_objects = formal_objects
        self.backup_store = backup_store
        self.config = config
        self.now = now

    def create_verified(
        self,
        request: PurgeObjectBackupRequest,
    ) -> VerifiedPurgeObjectBackup:
        asset_ids = _validate_request(request)
        database_manifest = self.restore_points.require_verified(
            request.purge_batch_id
        )
        _validate_restore_point(database_manifest, request.purge_batch_id)
        if database_manifest.remote_bucket != self.config.backup_bucket:
            raise PurgeObjectIntegrityError(
                "PostgreSQL 恢复点与对象备份 Bucket 身份不一致",
                stage="restore_point",
                error_code="restore_point_storage_mismatch",
            )

        batch_prefix = (
            f"{self.config.backup_prefix.strip('/')}/"
            f"{database_manifest.backup_id}/objects"
        )
        plan_key = f"{batch_prefix}/plan.json"
        manifest_key = f"{batch_prefix}/manifest.json"
        local_directory = (
            Path(self.config.local_root)
            / database_manifest.backup_id
            / "objects"
        )
        local_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(local_directory, 0o700)
        local_manifest_path = local_directory / "manifest.json"
        if local_manifest_path.exists():
            return self._reconcile_complete(
                request=request,
                asset_ids=asset_ids,
                database_manifest=database_manifest,
                batch_prefix=batch_prefix,
                plan_key=plan_key,
                manifest_key=manifest_key,
                local_directory=local_directory,
            )
        if self.backup_store.head(manifest_key) is not None:
            local_plan_path = local_directory / "plan.json"
            if not local_plan_path.is_file():
                raise PurgeObjectConflictError(
                    "远端 complete 对象备份缺少本机不可变 plan",
                    stage="local_reconcile",
                )
            manifest_bytes = _download_bounded_bytes(
                self.backup_store,
                manifest_key,
                maximum_size=1024 * 1024,
            )
            try:
                remote_manifest = PurgeObjectBackupManifest.from_dict(
                    json.loads(manifest_bytes)
                )
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise PurgeObjectIntegrityError(
                    "远端对象备份 manifest 无效",
                    stage="manifest_validate",
                    error_code="invalid_manifest",
                ) from exc
            if _json_bytes(remote_manifest.to_dict()) != manifest_bytes:
                raise PurgeObjectIntegrityError(
                    "远端对象备份 manifest 不是 canonical JSON",
                    stage="manifest_validate",
                    error_code="invalid_manifest",
                )
            _write_local_immutable(local_manifest_path, manifest_bytes)
            return self._reconcile_complete(
                request=request,
                asset_ids=asset_ids,
                database_manifest=database_manifest,
                batch_prefix=batch_prefix,
                plan_key=plan_key,
                manifest_key=manifest_key,
                local_directory=local_directory,
            )

        snapshot = self.references.capture_for_purge(asset_ids)
        self._require_fresh_snapshot(snapshot)
        planned, protected, snapshot_sha256 = _plan_snapshot(
            snapshot,
            asset_ids,
            formal_bucket=self.config.formal_bucket,
        )
        # plan 必须跨进程、跨时间重试保持字节稳定；使用同批次数据库恢复点
        # 的不可变完成时间，而不是每次重试都会变化的墙钟时间。
        created_at = _as_utc(database_manifest.completed_at)

        with tempfile.TemporaryDirectory(
            prefix="prepare-",
            dir=local_directory,
        ) as temporary_name:
            temporary_directory = Path(temporary_name)
            os.chmod(temporary_directory, 0o700)
            prepared = tuple(
                self._prepare_source_object(
                    item,
                    temporary_directory,
                    batch_prefix=batch_prefix,
                )
                for item in planned
            )
            plan_payload = _plan_payload(
                request=request,
                database_manifest=database_manifest,
                asset_ids=asset_ids,
                snapshot_sha256=snapshot_sha256,
                prepared=prepared,
                protected=protected,
                plan_key=plan_key,
                manifest_key=manifest_key,
                created_at=created_at,
            )
            plan_bytes = _json_bytes(plan_payload)
            _write_local_immutable(local_directory / "plan.json", plan_bytes)
            self._store_bytes_verified(
                plan_key,
                plan_bytes,
                metadata={
                    "purge-batch-id": request.purge_batch_id,
                    "kind": "purge-object-plan",
                    "sha256": hashlib.sha256(plan_bytes).hexdigest(),
                },
                conflict_stage="plan_reconcile",
            )

            copied = tuple(
                self._store_payload(item, request.purge_batch_id)
                for item in prepared
            )

            final_snapshot = self.references.capture_for_purge(asset_ids)
            self._require_fresh_snapshot(final_snapshot)
            _, _, final_snapshot_sha256 = _plan_snapshot(
                final_snapshot,
                asset_ids,
                formal_bucket=self.config.formal_bucket,
            )
            if final_snapshot_sha256 != snapshot_sha256:
                raise PurgeObjectReferenceError(
                    "对象复制期间实时引用关系已变化",
                    error_code="reference_snapshot_changed",
                )

            completed_at = max(_as_utc(self.now()), created_at)
            manifest = PurgeObjectBackupManifest(
                schema_version=MANIFEST_SCHEMA_VERSION,
                status="complete",
                kind="purge_object_backup",
                purge_batch_id=request.purge_batch_id,
                database_restore_point=_database_binding(database_manifest),
                asset_ids=asset_ids,
                reference_catalog_version=REFERENCE_CATALOG_VERSION,
                reference_snapshot_sha256=snapshot_sha256,
                plan_key=plan_key,
                manifest_key=manifest_key,
                objects=copied,
                reference_protected=protected,
                retention={
                    "days": RETENTION_DAYS,
                    "retain_until": _iso(database_manifest.retain_until),
                },
                created_at=created_at,
                completed_at=completed_at,
                authorization="backup_only_no_delete",
                production_gates=_production_gates(),
            )
            _validate_object_manifest(manifest)
            manifest_bytes = _json_bytes(manifest.to_dict())
            self._store_bytes_verified(
                manifest_key,
                manifest_bytes,
                metadata={
                    "purge-batch-id": request.purge_batch_id,
                    "kind": "purge-object-manifest",
                    "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                },
                conflict_stage="manifest_reconcile",
            )
            _write_local_immutable(
                local_directory / "manifest.json",
                manifest_bytes,
            )
        return VerifiedPurgeObjectBackup(
            status="complete",
            manifest_key=manifest_key,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            manifest=manifest,
        )

    def revalidate_current_candidates(
        self,
        manifest: PurgeObjectBackupManifest,
    ) -> CurrentDeletionCandidates:
        """只缩减已备份对象集合；绝不补入未备份的新候选。"""

        _validate_object_manifest(manifest)
        if any(
            item.formal_bucket != self.config.formal_bucket
            or item.backup_bucket != self.config.backup_bucket
            for item in manifest.objects
        ) or any(
            item.formal_bucket != self.config.formal_bucket
            for item in manifest.reference_protected
        ):
            raise PurgeObjectConflictError(
                "对象备份 manifest 与当前存储身份冲突",
                stage="current_revalidation",
            )

        snapshot = self.references.capture_for_purge(manifest.asset_ids)
        self._require_fresh_snapshot(snapshot)
        planned, _, snapshot_sha256 = _plan_snapshot(
            snapshot,
            manifest.asset_ids,
            formal_bucket=self.config.formal_bucket,
        )
        backed = {
            (item.kind, item.formal_key): item
            for item in manifest.objects
        }
        current: list[PurgeObjectBackupItem] = []
        for item in planned:
            existing = backed.get((item.kind, item.formal_key))
            if existing is None or existing.asset_ids != item.asset_ids:
                raise PurgeObjectReferenceError(
                    "当前引用关系产生了未备份的新删除候选",
                    error_code="unbacked_current_candidate",
                )
            current.append(existing)
        return CurrentDeletionCandidates(
            status="current",
            purge_batch_id=manifest.purge_batch_id,
            reference_snapshot_sha256=snapshot_sha256,
            objects=tuple(current),
        )

    def _reconcile_complete(
        self,
        *,
        request: PurgeObjectBackupRequest,
        asset_ids: tuple[str, ...],
        database_manifest: BackupManifest,
        batch_prefix: str,
        plan_key: str,
        manifest_key: str,
        local_directory: Path,
    ) -> VerifiedPurgeObjectBackup:
        local_manifest_path = local_directory / "manifest.json"
        local_plan_path = local_directory / "plan.json"
        if not local_plan_path.is_file():
            raise PurgeObjectConflictError(
                "complete 对象备份缺少本机不可变 plan",
                stage="local_reconcile",
            )
        try:
            manifest_bytes = local_manifest_path.read_bytes()
            manifest = PurgeObjectBackupManifest.from_dict(
                json.loads(manifest_bytes)
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PurgeObjectIntegrityError(
                "本机对象备份 manifest 无法读取",
                stage="manifest_validate",
                error_code="invalid_manifest",
            ) from exc
        if _json_bytes(manifest.to_dict()) != manifest_bytes:
            raise PurgeObjectIntegrityError(
                "本机对象备份 manifest 不是 canonical JSON",
                stage="manifest_validate",
                error_code="invalid_manifest",
            )

        expected_database = _database_binding(database_manifest)
        if (
            manifest.purge_batch_id != request.purge_batch_id
            or manifest.asset_ids != asset_ids
            or dict(manifest.database_restore_point) != dict(expected_database)
            or manifest.plan_key != plan_key
            or manifest.manifest_key != manifest_key
            or manifest.retention
            != {
                "days": RETENTION_DAYS,
                "retain_until": _iso(database_manifest.retain_until),
            }
        ):
            raise PurgeObjectConflictError(
                "既有对象备份 manifest 与当前批次绑定冲突",
                stage="manifest_reconcile",
            )
        for item in manifest.objects:
            if (
                item.formal_bucket != self.config.formal_bucket
                or item.backup_bucket != self.config.backup_bucket
                or not item.backup_key.startswith(f"{batch_prefix}/payloads/")
            ):
                raise PurgeObjectConflictError(
                    "既有对象备份 manifest 的 Bucket 或前缀冲突",
                    stage="manifest_reconcile",
                )

        snapshot = self.references.capture_for_purge(asset_ids)
        self._require_fresh_snapshot(snapshot)
        planned, protected, snapshot_sha256 = _plan_snapshot(
            snapshot,
            asset_ids,
            formal_bucket=self.config.formal_bucket,
        )
        expected_objects = {
            (
                item.kind,
                item.asset_ids,
                item.formal_key,
                item.selected_reference_count,
                item.total_reference_count,
                item.remaining_reference_count,
                item.reference_set_sha256,
            )
            for item in planned
        }
        actual_objects = {
            (
                item.kind,
                item.asset_ids,
                item.formal_key,
                item.selected_reference_count,
                item.total_reference_count,
                item.remaining_reference_count,
                item.reference_set_sha256,
            )
            for item in manifest.objects
        }
        if (
            snapshot_sha256 != manifest.reference_snapshot_sha256
            or expected_objects != actual_objects
            or tuple(item.to_dict() for item in protected)
            != tuple(item.to_dict() for item in manifest.reference_protected)
        ):
            raise PurgeObjectReferenceError(
                "既有对象备份与当前实时引用关系不一致",
                error_code="reference_snapshot_changed",
            )

        manifest_objects = {
            (item.kind, item.formal_key): item
            for item in manifest.objects
        }
        with tempfile.TemporaryDirectory(
            prefix="reconcile-source-",
            dir=local_directory,
        ) as temporary_name:
            temporary_directory = Path(temporary_name)
            os.chmod(temporary_directory, 0o700)
            for current in planned:
                prepared = self._prepare_source_object(
                    current,
                    temporary_directory,
                    batch_prefix=batch_prefix,
                )
                recorded = manifest_objects[(current.kind, current.formal_key)]
                if (
                    prepared.object_id != recorded.object_id
                    or prepared.backup_key != recorded.backup_key
                    or prepared.size != recorded.size
                    or prepared.sha256 != recorded.sha256
                ):
                    raise PurgeObjectIntegrityError(
                        "正式对象当前字节与已完成备份不一致",
                        stage="source_reconcile",
                        error_code="source_object_changed",
                    )

        plan_bytes = local_plan_path.read_bytes()
        self._verify_stored_object(
            plan_key,
            expected_size=len(plan_bytes),
            expected_sha256=hashlib.sha256(plan_bytes).hexdigest(),
            expected_metadata={
                "purge-batch-id": request.purge_batch_id,
                "kind": "purge-object-plan",
                "sha256": hashlib.sha256(plan_bytes).hexdigest(),
            },
            conflict_stage="plan_reconcile",
        )
        for item in manifest.objects:
            self._verify_stored_object(
                item.backup_key,
                expected_size=item.size,
                expected_sha256=item.sha256,
                expected_metadata={
                    "purge-batch-id": request.purge_batch_id,
                    "object-id": item.object_id,
                    "object-kind": item.kind,
                    "sha256": item.sha256,
                    "retention-days": str(RETENTION_DAYS),
                },
                conflict_stage="backup_reconcile",
            )
        self._verify_stored_object(
            manifest_key,
            expected_size=len(manifest_bytes),
            expected_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            expected_metadata={
                "purge-batch-id": request.purge_batch_id,
                "kind": "purge-object-manifest",
                "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            },
            conflict_stage="manifest_reconcile",
        )
        return VerifiedPurgeObjectBackup(
            status="complete",
            manifest_key=manifest_key,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            manifest=manifest,
        )

    def _require_fresh_snapshot(
        self,
        snapshot: CompleteReferenceSnapshot,
    ) -> None:
        try:
            captured_at = _as_utc(snapshot.captured_at)
            checked_at = _as_utc(self.now())
        except PurgeObjectConfigError as exc:
            raise PurgeObjectReferenceError(
                "实时引用快照时间无效",
                error_code="reference_snapshot_stale",
            ) from exc
        age_seconds = (checked_at - captured_at).total_seconds()
        if (
            age_seconds > self.config.reference_snapshot_max_age_seconds
            or age_seconds < -60
        ):
            raise PurgeObjectReferenceError(
                "实时引用快照已过期或来自未来",
                error_code="reference_snapshot_stale",
            )

    def _prepare_source_object(
        self,
        planned: _PlannedObject,
        temporary_directory: Path,
        *,
        batch_prefix: str,
    ) -> _PreparedObject:
        found = self.formal_objects.head(planned.formal_key)
        if found is None:
            raise PurgeObjectIntegrityError(
                "正式 OSS 对象不存在",
                stage="source_head",
                error_code="source_object_missing",
            )
        object_id = hashlib.sha256(
            (
                self.config.formal_bucket
                + "\0"
                + planned.formal_key
            ).encode("utf-8")
        ).hexdigest()
        path = temporary_directory / object_id
        with path.open("xb") as target:
            os.chmod(path, 0o600)
            self.formal_objects.download_to(planned.formal_key, target)
            target.flush()
            os.fsync(target.fileno())
        size, sha256, md5 = _hash_file(path)
        if found.size != size:
            raise PurgeObjectIntegrityError(
                "正式 OSS HEAD 与下载大小不一致",
                stage="source_download",
                error_code="source_size_mismatch",
            )
        metadata = {
            str(name).lower(): str(value)
            for name, value in found.metadata.items()
        }
        if planned.kind == "source_image":
            if (
                planned.expected_size != size
                or planned.expected_sha256 != sha256
                or metadata.get("sha256") != sha256
                or metadata.get("source-size") != str(size)
            ):
                raise PurgeObjectIntegrityError(
                    "正式源图大小或哈希不匹配",
                    stage="source_download",
                    error_code="source_hash_mismatch",
                )
        elif (
            metadata.get("preview-size") != str(size)
            or metadata.get("preview-md5", "").lower() != md5
        ):
            raise PurgeObjectIntegrityError(
                "正式搜索预览图 metadata 与实际字节不匹配",
                stage="source_download",
                error_code="source_hash_mismatch",
            )
        return _PreparedObject(
            planned=planned,
            object_id=object_id,
            backup_key=(
                f"{batch_prefix}/payloads/{planned.kind}/{object_id}"
            ),
            path=path,
            size=size,
            sha256=sha256,
        )

    def _store_payload(
        self,
        prepared: _PreparedObject,
        purge_batch_id: str,
    ) -> PurgeObjectBackupItem:
        metadata = {
            "purge-batch-id": purge_batch_id,
            "object-id": prepared.object_id,
            "object-kind": prepared.planned.kind,
            "sha256": prepared.sha256,
            "retention-days": str(RETENTION_DAYS),
        }
        existing = self.backup_store.head(prepared.backup_key)
        if existing is None:
            try:
                self.backup_store.put_file_if_absent(
                    prepared.backup_key,
                    prepared.path,
                    metadata=metadata,
                )
            except BackupStorageConflictError:
                pass
            except BackupStorageError as exc:
                raise PurgeObjectIntegrityError(
                    "对象备份写入失败",
                    stage="backup_put",
                    error_code="backup_storage_failed",
                ) from exc
        self._verify_stored_object(
            prepared.backup_key,
            expected_size=prepared.size,
            expected_sha256=prepared.sha256,
            expected_metadata=metadata,
            conflict_stage="backup_reconcile",
        )
        return PurgeObjectBackupItem(
            object_id=prepared.object_id,
            kind=prepared.planned.kind,
            asset_ids=prepared.planned.asset_ids,
            formal_bucket=self.config.formal_bucket,
            formal_key=prepared.planned.formal_key,
            backup_bucket=self.config.backup_bucket,
            backup_key=prepared.backup_key,
            size=prepared.size,
            sha256=prepared.sha256,
            selected_reference_count=(
                prepared.planned.selected_reference_count
            ),
            total_reference_count=prepared.planned.total_reference_count,
            remaining_reference_count=(
                prepared.planned.remaining_reference_count
            ),
            reference_set_sha256=prepared.planned.reference_set_sha256,
            verification={
                "source_head_download": "passed",
                "backup_head": "passed",
                "backup_download_sha256": "passed",
            },
        )

    def _store_bytes_verified(
        self,
        key: str,
        data: bytes,
        *,
        metadata: Mapping[str, str],
        conflict_stage: str,
    ) -> None:
        existing = self.backup_store.head(key)
        if existing is None:
            try:
                self.backup_store.put_bytes_if_absent(
                    key,
                    data,
                    metadata=metadata,
                )
            except BackupStorageConflictError:
                pass
            except BackupStorageError as exc:
                raise PurgeObjectIntegrityError(
                    "对象备份清单写入失败",
                    stage="backup_put",
                    error_code="backup_storage_failed",
                ) from exc
        self._verify_stored_object(
            key,
            expected_size=len(data),
            expected_sha256=hashlib.sha256(data).hexdigest(),
            expected_metadata=metadata,
            conflict_stage=conflict_stage,
        )

    def _verify_stored_object(
        self,
        key: str,
        *,
        expected_size: int,
        expected_sha256: str,
        expected_metadata: Mapping[str, str],
        conflict_stage: str,
    ) -> None:
        found = self.backup_store.head(key)
        normalized = (
            {
                str(name).lower(): str(value)
                for name, value in found.metadata.items()
            }
            if found is not None
            else {}
        )
        if (
            found is None
            or found.size != expected_size
            or any(
                normalized.get(str(name).lower()) != str(value)
                for name, value in expected_metadata.items()
            )
        ):
            raise PurgeObjectConflictError(
                "备份对象 HEAD 身份、大小或 metadata 冲突",
                stage=conflict_stage,
            )
        actual_size, actual_sha256 = _download_size_sha256(
            self.backup_store,
            key,
        )
        if actual_size != expected_size or actual_sha256 != expected_sha256:
            raise PurgeObjectConflictError(
                "备份对象下载内容冲突",
                stage=conflict_stage,
            )


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KIND_ORDER = {"source_image": 0, "search_preview": 1}


def _validate_request(request: PurgeObjectBackupRequest) -> tuple[str, ...]:
    BackupRequest.restore_point(request.purge_batch_id)
    if not 1 <= len(request.asset_ids) <= 20:
        raise PurgeObjectConfigError(
            "永久清除对象备份每批只允许 1 至 20 张图片资产",
            error_code="invalid_asset_count",
        )
    if len(set(request.asset_ids)) != len(request.asset_ids):
        raise PurgeObjectConfigError(
            "图片资产 ID 不得重复",
            error_code="duplicate_asset_id",
        )
    if any(not _SAFE_IDENTIFIER.fullmatch(value) for value in request.asset_ids):
        raise PurgeObjectConfigError(
            "图片资产 ID 包含不安全字符",
            error_code="invalid_asset_id",
        )
    return tuple(sorted(request.asset_ids))


def _validate_restore_point(manifest: BackupManifest, purge_batch_id: str) -> None:
    validate_manifest_contract(manifest)
    expected = BackupRequest.restore_point(purge_batch_id)
    if (
        manifest.kind != expected.kind
        or manifest.purge_batch_id != purge_batch_id
        or manifest.backup_id != expected.backup_id
        or manifest.retention_days != RETENTION_DAYS
    ):
        raise PurgeObjectIntegrityError(
            "PostgreSQL 恢复点与永久清除批次绑定不匹配",
            stage="restore_point",
            error_code="restore_point_batch_mismatch",
        )


def _plan_snapshot(
    snapshot: CompleteReferenceSnapshot,
    asset_ids: tuple[str, ...],
    *,
    formal_bucket: str,
) -> tuple[
    tuple[_PlannedObject, ...],
    tuple[ReferenceProtectedObject, ...],
    str,
]:
    _validate_snapshot(snapshot, asset_ids)
    targets = {target.asset_id: target for target in snapshot.targets}
    references_by_key: dict[str, list[ObjectReference]] = {}
    roles_by_key: dict[str, set[str]] = {}
    for reference in snapshot.references:
        references_by_key.setdefault(reference.formal_key, []).append(reference)
        roles_by_key.setdefault(reference.formal_key, set()).add(reference.kind)
    if any(len(roles) != 1 for roles in roles_by_key.values()):
        raise PurgeObjectReferenceError(
            "同一正式对象被登记为不同对象类型",
            error_code="mixed_object_role",
        )

    planned: list[_PlannedObject] = []
    protected: list[ReferenceProtectedObject] = []
    for asset_id in asset_ids:
        target = targets[asset_id]
        original_references = references_by_key.get(target.original_key, [])
        if (
            len(original_references) != 1
            or original_references[0].source != "image_assets"
            or original_references[0].owner_id != asset_id
            or original_references[0].kind != "source_image"
        ):
            raise PurgeObjectReferenceError(
                "正式源图不是当前目标资产的独占引用",
                error_code="source_image_not_exclusive",
            )
        planned.append(
            _PlannedObject(
                kind="source_image",
                asset_ids=(asset_id,),
                formal_key=target.original_key,
                expected_size=target.original_size,
                expected_sha256=target.original_sha256,
                normalization_version=None,
                selected_reference_count=1,
                total_reference_count=len(original_references),
                remaining_reference_count=0,
                reference_set_sha256=_reference_set_sha256(
                    original_references
                ),
            )
        )

    preview_targets: dict[str, list[PurgeAssetSnapshot]] = {}
    for target in targets.values():
        preview_targets.setdefault(target.preview_key, []).append(target)
    selected = set(asset_ids)
    for preview_key, owners in preview_targets.items():
        target_owner_ids = tuple(sorted(owner.asset_id for owner in owners))
        preview_references = references_by_key.get(preview_key, [])
        target_edges = {
            reference.owner_id
            for reference in preview_references
            if reference.source == "image_assets"
            and reference.kind == "search_preview"
            and reference.owner_id in selected
        }
        if target_edges != set(target_owner_ids):
            raise PurgeObjectReferenceError(
                "目标资产缺少搜索预览图引用",
                error_code="target_preview_reference_missing",
            )
        remaining = [
            reference
            for reference in preview_references
            if not (
                reference.source == "image_assets"
                and reference.owner_id in selected
            )
        ]
        if remaining:
            protected.append(
                ReferenceProtectedObject(
                    kind="search_preview",
                    formal_bucket=formal_bucket,
                    formal_key=preview_key,
                    selected_asset_ids=target_owner_ids,
                    selected_reference_count=len(target_edges),
                    total_reference_count=len(preview_references),
                    remaining_reference_count=len(remaining),
                    reference_set_sha256=_reference_set_sha256(
                        preview_references
                    ),
                )
            )
        else:
            versions = {owner.normalization_version for owner in owners}
            if len(versions) != 1:
                raise PurgeObjectReferenceError(
                    "共享搜索预览图的规范化版本不一致",
                    error_code="preview_binding_conflict",
                )
            planned.append(
                _PlannedObject(
                    kind="search_preview",
                    asset_ids=target_owner_ids,
                    formal_key=preview_key,
                    expected_size=None,
                    expected_sha256=None,
                    normalization_version=next(iter(versions)),
                    selected_reference_count=len(target_edges),
                    total_reference_count=len(preview_references),
                    remaining_reference_count=0,
                    reference_set_sha256=_reference_set_sha256(
                        preview_references
                    ),
                )
            )
    planned.sort(key=lambda item: (_KIND_ORDER[item.kind], item.formal_key))
    protected.sort(key=lambda item: item.formal_key)
    snapshot_sha256 = hashlib.sha256(
        _json_bytes(_snapshot_semantics(snapshot, planned, protected))
    ).hexdigest()
    return tuple(planned), tuple(protected), snapshot_sha256


def _validate_snapshot(
    snapshot: CompleteReferenceSnapshot,
    asset_ids: tuple[str, ...],
) -> None:
    if snapshot.catalog_version != REFERENCE_CATALOG_VERSION:
        raise PurgeObjectReferenceError(
            "实时引用目录版本不受支持",
            error_code="reference_catalog_mismatch",
        )
    if not snapshot.consistency_token:
        raise PurgeObjectReferenceError("实时引用快照缺少一致性标识")
    target_ids = tuple(sorted(target.asset_id for target in snapshot.targets))
    if target_ids != asset_ids or len(target_ids) != len(set(target_ids)):
        raise PurgeObjectReferenceError(
            "实时引用快照目标与请求不一致",
            error_code="reference_targets_mismatch",
        )
    if any(target.status != "archived" for target in snapshot.targets):
        raise PurgeObjectReferenceError(
            "永久清除对象备份只接受已归档图片",
            error_code="asset_not_archived",
        )
    for target in snapshot.targets:
        _validate_key(target.original_key)
        _validate_key(target.preview_key)
        if (
            target.original_key == target.preview_key
            or target.original_size <= 0
            or not _SHA256.fullmatch(target.original_sha256)
            or not target.normalization_version
        ):
            raise PurgeObjectReferenceError("目标资产对象绑定无效")

    slices = {item.source: item for item in snapshot.source_slices}
    if (
        len(slices) != len(snapshot.source_slices)
        or set(slices) != REQUIRED_REFERENCE_SOURCES
    ):
        raise PurgeObjectReferenceError(
            "实时引用快照未覆盖全部引用来源",
            error_code="reference_sources_incomplete",
        )
    targets_by_id = {target.asset_id: target for target in snapshot.targets}
    counts = {source: 0 for source in REQUIRED_REFERENCE_SOURCES}
    seen_references: set[tuple[str, str, str, str, str]] = set()
    for reference in snapshot.references:
        if reference.source not in REQUIRED_REFERENCE_SOURCES:
            raise PurgeObjectReferenceError(
                "实时引用快照包含未知引用来源",
                error_code="unknown_reference_source",
            )
        if reference.kind not in _KIND_ORDER:
            raise PurgeObjectReferenceError("实时引用对象类型无效")
        if (
            reference.owner_state
            not in REFERENCE_OWNER_STATES[reference.source]
            or not _SAFE_IDENTIFIER.fullmatch(reference.owner_id)
            or (
                reference.source == "image_import_items"
                and reference.kind != "search_preview"
            )
            or (
                reference.source == "image_assets"
                and reference.owner_id in targets_by_id
                and reference.owner_state
                != targets_by_id[reference.owner_id].status
            )
        ):
            raise PurgeObjectReferenceError(
                "实时引用所有者状态或类型不在目录合同内",
                error_code="invalid_reference_state",
            )
        _validate_key(reference.formal_key)
        identity = (
            reference.source,
            reference.owner_id,
            reference.owner_state,
            reference.kind,
            reference.formal_key,
        )
        if identity in seen_references:
            raise PurgeObjectReferenceError(
                "实时引用快照包含重复引用边",
                error_code="duplicate_reference",
            )
        seen_references.add(identity)
        counts[reference.source] += 1
    for source, source_slice in slices.items():
        if (
            source_slice.consistency_token != snapshot.consistency_token
            or source_slice.status != "complete"
            or source_slice.truncated
            or source_slice.enumerated_count != counts[source]
        ):
            raise PurgeObjectReferenceError(
                "实时引用来源切片不完整或不一致",
                error_code="reference_slice_incomplete",
            )


def _snapshot_semantics(
    snapshot: CompleteReferenceSnapshot,
    planned: Sequence[_PlannedObject],
    protected: Sequence[ReferenceProtectedObject],
) -> Mapping[str, Any]:
    return {
        "catalog_version": snapshot.catalog_version,
        "targets": [
            {
                "asset_id": item.asset_id,
                "status": item.status,
                "original_key": item.original_key,
                "preview_key": item.preview_key,
                "original_size": item.original_size,
                "original_sha256": item.original_sha256,
                "normalization_version": item.normalization_version,
            }
            for item in sorted(snapshot.targets, key=lambda value: value.asset_id)
        ],
        "sources": [
            {
                "source": item.source,
                "status": item.status,
                "truncated": item.truncated,
                "enumerated_count": item.enumerated_count,
            }
            for item in sorted(snapshot.source_slices, key=lambda value: value.source)
        ],
        "references": [
            {
                "source": item.source,
                "owner_id": item.owner_id,
                "owner_state": item.owner_state,
                "kind": item.kind,
                "formal_key": item.formal_key,
            }
            for item in sorted(
                snapshot.references,
                key=lambda value: (
                    value.source,
                    value.owner_id,
                    value.kind,
                    value.formal_key,
                ),
            )
        ],
        "planned": [
            {
                "kind": item.kind,
                "asset_ids": list(item.asset_ids),
                "formal_key": item.formal_key,
                "selected_reference_count": item.selected_reference_count,
                "total_reference_count": item.total_reference_count,
                "remaining_reference_count": item.remaining_reference_count,
                "reference_set_sha256": item.reference_set_sha256,
            }
            for item in planned
        ],
        "reference_protected": [item.to_dict() for item in protected],
    }


def _plan_payload(
    *,
    request: PurgeObjectBackupRequest,
    database_manifest: BackupManifest,
    asset_ids: tuple[str, ...],
    snapshot_sha256: str,
    prepared: Sequence[_PreparedObject],
    protected: Sequence[ReferenceProtectedObject],
    plan_key: str,
    manifest_key: str,
    created_at: datetime,
) -> Mapping[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "status": "planned",
        "kind": "purge_object_backup",
        "purge_batch_id": request.purge_batch_id,
        "database_restore_point": _database_binding(database_manifest),
        "selection": {
            "asset_ids": list(asset_ids),
            "reference_catalog_version": REFERENCE_CATALOG_VERSION,
            "reference_snapshot_sha256": snapshot_sha256,
            "reference_protected": [item.to_dict() for item in protected],
        },
        "copies": {
            "plan_key": plan_key,
            "manifest_key": manifest_key,
            "objects": [
                {
                    "object_id": item.object_id,
                    "kind": item.planned.kind,
                    "asset_ids": list(item.planned.asset_ids),
                    "formal": {"key": item.planned.formal_key},
                    "backup": {"key": item.backup_key},
                    "size_bytes": item.size,
                    "sha256": item.sha256,
                    "reference_evidence": {
                        "selected_count": (
                            item.planned.selected_reference_count
                        ),
                        "total_count": item.planned.total_reference_count,
                        "remaining_count": (
                            item.planned.remaining_reference_count
                        ),
                        "reference_set_sha256": (
                            item.planned.reference_set_sha256
                        ),
                    },
                }
                for item in prepared
            ],
        },
        "retention": {
            "days": RETENTION_DAYS,
            "retain_until": _iso(database_manifest.retain_until),
        },
        "created_at": _iso(created_at),
        "authorization": "backup_only_no_delete",
    }


def _database_binding(manifest: BackupManifest) -> Mapping[str, Any]:
    manifest_bytes = _json_bytes(manifest.to_dict())
    return {
        "backup_id": manifest.backup_id,
        "purge_batch_id": manifest.purge_batch_id,
        "remote_bucket": manifest.remote_bucket,
        "remote_manifest_key": manifest.remote_manifest_key,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "artifact_sha256": manifest.artifact_sha256,
        "completed_at": _iso(manifest.completed_at),
        "retain_until": _iso(manifest.retain_until),
    }


def _reference_set_sha256(
    references: Sequence[ObjectReference],
) -> str:
    payload = [
        {
            "source": item.source,
            "owner_id": item.owner_id,
            "owner_state": item.owner_state,
            "kind": item.kind,
            "formal_key": item.formal_key,
        }
        for item in sorted(
            references,
            key=lambda value: (
                value.source,
                value.owner_id,
                value.owner_state,
                value.kind,
                value.formal_key,
            ),
        )
    ]
    return hashlib.sha256(_json_bytes({"references": payload})).hexdigest()


def _production_gates() -> Mapping[str, str]:
    return {
        "source_read_only_credential": "not_verified",
        "backup_private_sse": "not_verified",
        "backup_credential_no_delete": "not_verified",
        "retention_policy_30_days": "not_verified",
        "isolated_restore_drill": "not_verified",
    }


def _require_exact_keys(
    payload: Mapping[str, Any],
    expected: set[str],
) -> None:
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise ValueError("unexpected manifest fields")


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) for item in value
    )


def _parse_datetime(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid datetime")
    parsed = datetime.fromisoformat(
        value[:-1] + "+00:00" if value.endswith("Z") else value
    )
    if parsed.tzinfo is None:
        raise ValueError("datetime must include timezone")
    return parsed.astimezone(timezone.utc)


def _validate_object_manifest(manifest: PurgeObjectBackupManifest) -> None:
    def invalid() -> None:
        raise PurgeObjectIntegrityError(
            "对象备份 manifest schema 或内容无效",
            stage="manifest_validate",
            error_code="invalid_manifest",
        )

    database = manifest.database_restore_point
    try:
        database_completed_at = _parse_datetime(
            str(database["completed_at"])
        )
        database_retain_until = _parse_datetime(
            str(database["retain_until"])
        )
    except (KeyError, TypeError, ValueError):
        invalid()
        return

    if (
        manifest.schema_version != MANIFEST_SCHEMA_VERSION
        or manifest.status != "complete"
        or manifest.kind != "purge_object_backup"
        or not _SAFE_IDENTIFIER.fullmatch(manifest.purge_batch_id)
        or database.get("purge_batch_id") != manifest.purge_batch_id
        or database.get("backup_id") != f"purge-{manifest.purge_batch_id}"
        or not _is_safe_bucket(str(database.get("remote_bucket", "")))
        or not isinstance(database.get("remote_manifest_key"), str)
        or not _is_safe_key(str(database.get("remote_manifest_key")))
        or not _SHA256.fullmatch(str(database.get("manifest_sha256", "")))
        or not _SHA256.fullmatch(str(database.get("artifact_sha256", "")))
        or database.get("completed_at") != _iso(database_completed_at)
        or database.get("retain_until") != _iso(database_retain_until)
        or database_retain_until <= database_completed_at
        or not 1 <= len(manifest.asset_ids) <= 20
        or manifest.asset_ids != tuple(sorted(manifest.asset_ids))
        or len(set(manifest.asset_ids)) != len(manifest.asset_ids)
        or any(
            not _SAFE_IDENTIFIER.fullmatch(asset_id)
            for asset_id in manifest.asset_ids
        )
        or manifest.reference_catalog_version != REFERENCE_CATALOG_VERSION
        or not _SHA256.fullmatch(manifest.reference_snapshot_sha256)
        or not _is_safe_key(manifest.plan_key)
        or not _is_safe_key(manifest.manifest_key)
        or not manifest.plan_key.endswith("/plan.json")
        or not manifest.manifest_key.endswith("/manifest.json")
        or manifest.plan_key[: -len("plan.json")]
        != manifest.manifest_key[: -len("manifest.json")]
        or manifest.plan_key == manifest.manifest_key
        or manifest.retention
        != {
            "days": RETENTION_DAYS,
            "retain_until": str(database["retain_until"]),
        }
        or manifest.created_at > manifest.completed_at
        or database_retain_until <= manifest.completed_at
        or manifest.authorization != "backup_only_no_delete"
        or dict(manifest.production_gates) != dict(_production_gates())
        or not 1 <= len(manifest.objects) <= 40
        or len(manifest.reference_protected) > 20
    ):
        invalid()

    selected_assets = set(manifest.asset_ids)
    formal_identities: set[tuple[str, str]] = set()
    backup_identities: set[tuple[str, str]] = set()
    object_ids: set[str] = set()
    original_counts = {asset_id: 0 for asset_id in manifest.asset_ids}
    copied_base = manifest.manifest_key[: -len("manifest.json")]
    for item in manifest.objects:
        expected_object_id = hashlib.sha256(
            (item.formal_bucket + "\0" + item.formal_key).encode("utf-8")
        ).hexdigest()
        if (
            item.object_id != expected_object_id
            or item.object_id in object_ids
            or item.kind not in _KIND_ORDER
            or not item.asset_ids
            or item.asset_ids != tuple(sorted(item.asset_ids))
            or len(set(item.asset_ids)) != len(item.asset_ids)
            or not set(item.asset_ids).issubset(selected_assets)
            or not _is_safe_bucket(item.formal_bucket)
            or not _is_safe_key(item.formal_key)
            or not _is_safe_bucket(item.backup_bucket)
            or item.backup_bucket != database["remote_bucket"]
            or not _is_safe_key(item.backup_key)
            or item.backup_key
            != f"{copied_base}payloads/{item.kind}/{item.object_id}"
            or item.size <= 0
            or not _SHA256.fullmatch(item.sha256)
            or item.selected_reference_count != len(item.asset_ids)
            or item.total_reference_count < item.selected_reference_count
            or item.remaining_reference_count
            != item.total_reference_count - item.selected_reference_count
            or item.remaining_reference_count != 0
            or not _SHA256.fullmatch(item.reference_set_sha256)
            or dict(item.verification)
            != {
                "source_head_download": "passed",
                "backup_head": "passed",
                "backup_download_sha256": "passed",
            }
            or (item.formal_bucket, item.formal_key) in formal_identities
            or (item.backup_bucket, item.backup_key) in backup_identities
        ):
            invalid()
        object_ids.add(item.object_id)
        formal_identities.add((item.formal_bucket, item.formal_key))
        backup_identities.add((item.backup_bucket, item.backup_key))
        if item.kind == "source_image":
            if len(item.asset_ids) != 1:
                invalid()
            original_counts[item.asset_ids[0]] += 1
    if any(count != 1 for count in original_counts.values()):
        invalid()

    protected_identities: set[tuple[str, str]] = set()
    for item in manifest.reference_protected:
        identity = (item.formal_bucket, item.formal_key)
        if (
            item.kind != "search_preview"
            or not item.selected_asset_ids
            or item.selected_asset_ids != tuple(sorted(item.selected_asset_ids))
            or len(set(item.selected_asset_ids))
            != len(item.selected_asset_ids)
            or not set(item.selected_asset_ids).issubset(selected_assets)
            or not _is_safe_bucket(item.formal_bucket)
            or not _is_safe_key(item.formal_key)
            or item.selected_reference_count
            != len(item.selected_asset_ids)
            or item.total_reference_count < item.selected_reference_count
            or item.remaining_reference_count
            != item.total_reference_count - item.selected_reference_count
            or item.remaining_reference_count <= 0
            or not _SHA256.fullmatch(item.reference_set_sha256)
            or identity in formal_identities
            or identity in protected_identities
        ):
            invalid()
        protected_identities.add(identity)


def _download_size_sha256(
    storage: ReadableObjectStorage,
    key: str,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with tempfile.TemporaryFile(mode="w+b") as target:
        storage.download_to(key, target)
        target.seek(0)
        while True:
            chunk = target.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _download_bounded_bytes(
    storage: ReadableObjectStorage,
    key: str,
    *,
    maximum_size: int,
) -> bytes:
    with tempfile.TemporaryFile(mode="w+b") as target:
        storage.download_to(key, target)
        size = target.tell()
        if size > maximum_size:
            raise PurgeObjectIntegrityError(
                "对象备份 manifest 超出安全大小限制",
                stage="manifest_validate",
                error_code="invalid_manifest",
            )
        target.seek(0)
        return target.read()


def _hash_file(path: Path) -> tuple[int, str, str]:
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            sha256.update(chunk)
            md5.update(chunk)
    return size, sha256.hexdigest(), md5.hexdigest()


def _write_local_immutable(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise PurgeObjectConflictError(
                "本机对象备份清单冲突",
                stage="local_reconcile",
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with path.open("xb") as target:
            os.chmod(path, 0o600)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
    except FileExistsError:
        if path.read_bytes() != data:
            raise PurgeObjectConflictError(
                "本机对象备份清单冲突",
                stage="local_reconcile",
            ) from None


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _validate_key(key: str) -> None:
    if not _is_safe_key(key):
        raise PurgeObjectConfigError("对象 Key 或前缀不安全")


def _is_safe_key(key: str) -> bool:
    return bool(
        isinstance(key, str)
        and key
        and not key.startswith("/")
        and "\\" not in key
        and not any(part in {"", ".", ".."} for part in key.split("/"))
        and not any(ord(character) < 32 for character in key)
    )


def _is_safe_bucket(bucket: str) -> bool:
    return bool(
        isinstance(bucket, str)
        and bucket
        and "/" not in bucket
        and "\\" not in bucket
        and not any(ord(character) < 32 for character in bucket)
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise PurgeObjectConfigError("时间必须包含时区")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")
