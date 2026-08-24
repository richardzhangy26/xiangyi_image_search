"""永久清除控制面：认证、安全门只读状态与拒绝写路径。"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import timezone

from flask import Blueprint, current_app, jsonify, request

from models import AssetActivityRecord, db
from services.admin_auth import AdminAuthError
from services.purge_safety_gate import (
    CONDITION_LABELS,
    GateNotReady,
    pipeline_available,
)

logger = logging.getLogger(__name__)

admin_purge_bp = Blueprint(
    "admin_purge",
    __name__,
    url_prefix="/api/admin/purge",
)

BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_AUTH_STATUS = {
    "AUTH_NOT_CONFIGURED": 401,
    "AUTH_REQUIRED": 401,
    "AUTH_FORBIDDEN": 403,
}

# #26 只许替换写路由第 4 步（pipeline_available 之后）。
# cancel/retry 同样执行 require_ready()；证据过期会挡住取消（fail-closed）。
# 豁免须另行授权，不得在 #26 内删除步骤 3。


def _request_id() -> str:
    return (request.headers.get("X-Request-ID") or uuid.uuid4().hex)[:64]


def _iso(value):
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _readiness_body(snapshot) -> dict:
    return {
        "purge_available": snapshot.ready,
        "pipeline_available": pipeline_available(),
        "checked_at": _iso(snapshot.checked_at),
        "conditions": [
            {
                "id": item.id,
                "label": CONDITION_LABELS[item.id],
                "status": item.status,
                "checked_at": _iso(item.checked_at),
                "expires_at": _iso(item.expires_at),
                "summary": item.summary,
            }
            for item in snapshot.conditions
        ],
    }


def _gate_after_state(snapshot, error_code=None) -> dict:
    state = {
        "purge_available": snapshot.ready,
        "pipeline_available": pipeline_available(),
        "conditions": [
            {"id": item.id, "status": item.status}
            for item in snapshot.conditions
        ],
    }
    if error_code is not None:
        state["error_code"] = error_code
    return state


def _record(**kwargs) -> None:
    db.session.add(AssetActivityRecord(source="api", **kwargs))


def _commit():
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({
            "error": "操作记录写入失败",
            "error_code": "PURGE_CONTROL_AUDIT_FAILED",
        }), 500
    return None


def _control_failed():
    db.session.rollback()
    logger.error(
        "purge.control.failed request_id=%s error_type=unclassified",
        _request_id(),
    )
    return jsonify({
        "error": "永久清除控制面暂时不可用",
        "error_code": "PURGE_CONTROL_FAILED",
    }), 500


def _denied(exc: AdminAuthError, request_id: str):
    _record(
        event_type="purge.auth.denied",
        target_type="purge_gate",
        target_id="purge-gate",
        request_id=request_id,
        actor_id=None,
        result="denied",
        error_code=exc.error_code,
        after_state={"error_code": exc.error_code},
    )
    failed = _commit()
    if failed is not None:
        return failed
    status = _AUTH_STATUS.get(exc.error_code, 401)
    return jsonify({"error": exc.message, "error_code": exc.error_code}), status


def _rejected(
    *,
    principal,
    request_id,
    event_type,
    target_type,
    target_id,
    batch_id,
    error_code,
    error,
    status,
    snapshot,
):
    after_state = (
        _gate_after_state(snapshot, error_code)
        if snapshot is not None
        else {"error_code": error_code}
    )
    _record(
        event_type=event_type,
        target_type=target_type,
        target_id=target_id,
        request_id=request_id,
        actor_id=principal.actor_id,
        batch_id=batch_id,
        result="rejected",
        error_code=error_code,
        after_state=after_state,
    )
    failed = _commit()
    if failed is not None:
        return failed
    body = {"error": error, "error_code": error_code}
    if snapshot is not None:
        body["readiness"] = _readiness_body(snapshot)
    return jsonify(body), status


@admin_purge_bp.get('/readiness')
def get_purge_readiness():
    request_id = _request_id()
    try:
        principal = current_app.config["ADMIN_AUTH"].authenticate(
            request.headers.get("Authorization")
        )
    except AdminAuthError as exc:
        return _denied(exc, request_id)
    try:
        snapshot = current_app.config["PURGE_SAFETY_GATE"].evaluate()
        _record(
            event_type="purge.readiness.read",
            target_type="purge_gate",
            target_id="purge-gate",
            request_id=request_id,
            actor_id=principal.actor_id,
            result="succeeded",
            after_state=_gate_after_state(snapshot),
        )
        failed = _commit()
        if failed is not None:
            return failed
        return jsonify(_readiness_body(snapshot)), 200
    except Exception:
        return _control_failed()


@admin_purge_bp.post('/batches')
def create_purge_batch():
    request_id = _request_id()
    try:
        principal = current_app.config["ADMIN_AUTH"].authenticate(
            request.headers.get("Authorization")
        )
    except AdminAuthError as exc:
        return _denied(exc, request_id)
    try:
        snapshot = current_app.config["PURGE_SAFETY_GATE"].require_ready()
        if not pipeline_available():
            return _rejected(
                principal=principal,
                request_id=request_id,
                event_type="purge.batch.create.rejected",
                target_type="purge_batch",
                target_id="unspecified",
                batch_id=None,
                error_code="PURGE_PIPELINE_UNAVAILABLE",
                error="永久清除流水线尚未开放",
                status=409,
                snapshot=snapshot,
            )
        return _rejected(
            principal=principal,
            request_id=request_id,
            event_type="purge.batch.create.rejected",
            target_type="purge_batch",
            target_id="unspecified",
            batch_id=None,
            error_code="PURGE_PIPELINE_UNAVAILABLE",
            error="永久清除流水线尚未开放",
            status=409,
            snapshot=snapshot,
        )
    except GateNotReady as exc:
        return _rejected(
            principal=principal,
            request_id=request_id,
            event_type="purge.batch.create.rejected",
            target_type="purge_batch",
            target_id="unspecified",
            batch_id=None,
            error_code="PURGE_GATE_NOT_READY",
            error="永久清除安全门未满足",
            status=409,
            snapshot=exc.snapshot,
        )
    except Exception:
        return _control_failed()


@admin_purge_bp.post('/batches/<batch_id>/cancel')
def cancel_purge_batch(batch_id):
    request_id = _request_id()
    try:
        principal = current_app.config["ADMIN_AUTH"].authenticate(
            request.headers.get("Authorization")
        )
    except AdminAuthError as exc:
        return _denied(exc, request_id)
    try:
        if BATCH_ID_RE.fullmatch(batch_id) is None:
            return _rejected(
                principal=principal,
                request_id=request_id,
                event_type="purge.batch.cancel.rejected",
                target_type="purge_batch",
                target_id="invalid",
                batch_id=None,
                error_code="INVALID_PURGE_BATCH_ID",
                error="清除批次标识无效",
                status=400,
                snapshot=None,
            )
        snapshot = current_app.config["PURGE_SAFETY_GATE"].require_ready()
        if not pipeline_available():
            return _rejected(
                principal=principal,
                request_id=request_id,
                event_type="purge.batch.cancel.rejected",
                target_type="purge_batch",
                target_id=batch_id,
                batch_id=batch_id,
                error_code="PURGE_PIPELINE_UNAVAILABLE",
                error="永久清除流水线尚未开放",
                status=409,
                snapshot=snapshot,
            )
        return _rejected(
            principal=principal,
            request_id=request_id,
            event_type="purge.batch.cancel.rejected",
            target_type="purge_batch",
            target_id=batch_id,
            batch_id=batch_id,
            error_code="PURGE_PIPELINE_UNAVAILABLE",
            error="永久清除流水线尚未开放",
            status=409,
            snapshot=snapshot,
        )
    except GateNotReady as exc:
        return _rejected(
            principal=principal,
            request_id=request_id,
            event_type="purge.batch.cancel.rejected",
            target_type="purge_batch",
            target_id=batch_id,
            batch_id=batch_id,
            error_code="PURGE_GATE_NOT_READY",
            error="永久清除安全门未满足",
            status=409,
            snapshot=exc.snapshot,
        )
    except Exception:
        return _control_failed()


@admin_purge_bp.post('/batches/<batch_id>/retry')
def retry_purge_batch(batch_id):
    request_id = _request_id()
    try:
        principal = current_app.config["ADMIN_AUTH"].authenticate(
            request.headers.get("Authorization")
        )
    except AdminAuthError as exc:
        return _denied(exc, request_id)
    try:
        if BATCH_ID_RE.fullmatch(batch_id) is None:
            return _rejected(
                principal=principal,
                request_id=request_id,
                event_type="purge.batch.retry.rejected",
                target_type="purge_batch",
                target_id="invalid",
                batch_id=None,
                error_code="INVALID_PURGE_BATCH_ID",
                error="清除批次标识无效",
                status=400,
                snapshot=None,
            )
        snapshot = current_app.config["PURGE_SAFETY_GATE"].require_ready()
        if not pipeline_available():
            return _rejected(
                principal=principal,
                request_id=request_id,
                event_type="purge.batch.retry.rejected",
                target_type="purge_batch",
                target_id=batch_id,
                batch_id=batch_id,
                error_code="PURGE_PIPELINE_UNAVAILABLE",
                error="永久清除流水线尚未开放",
                status=409,
                snapshot=snapshot,
            )
        return _rejected(
            principal=principal,
            request_id=request_id,
            event_type="purge.batch.retry.rejected",
            target_type="purge_batch",
            target_id=batch_id,
            batch_id=batch_id,
            error_code="PURGE_PIPELINE_UNAVAILABLE",
            error="永久清除流水线尚未开放",
            status=409,
            snapshot=snapshot,
        )
    except GateNotReady as exc:
        return _rejected(
            principal=principal,
            request_id=request_id,
            event_type="purge.batch.retry.rejected",
            target_type="purge_batch",
            target_id=batch_id,
            batch_id=batch_id,
            error_code="PURGE_GATE_NOT_READY",
            error="永久清除安全门未满足",
            status=409,
            snapshot=exc.snapshot,
        )
    except Exception:
        return _control_failed()
