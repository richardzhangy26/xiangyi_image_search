from pathlib import Path
import re

BACKEND = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (BACKEND / rel).read_text(encoding="utf-8")


def test_urls_and_error_codes_locked():
    source = _read("blueprints/admin_purge.py")
    for needle in (
        "@admin_purge_bp.get('/readiness')",
        "@admin_purge_bp.post('/batches')",
        "@admin_purge_bp.get('/batches')",
        "@admin_purge_bp.get('/batches/<batch_id>')",
        "@admin_purge_bp.post('/batches/<batch_id>/cancel')",
        "@admin_purge_bp.post('/batches/<batch_id>/retry')",
        'url_prefix="/api/admin/purge"',
        "AUTH_NOT_CONFIGURED",
        "AUTH_REQUIRED",
        "AUTH_FORBIDDEN",
        "INVALID_PURGE_BATCH_ID",
        "INVALID_PURGE_IDEMPOTENCY_KEY",
        "PURGE_GATE_NOT_READY",
        "PURGE_PIPELINE_UNAVAILABLE",
        "PURGE_CONTROL_AUDIT_FAILED",
        "PURGE_CONTROL_FAILED",
        "PURGE_BATCH_NOT_CANCELLABLE",
        "PURGE_BATCH_NOT_RETRYABLE",
    ):
        assert needle in source


def test_write_handlers_keep_decision_order():
    source = _read("blueprints/admin_purge.py")
    for marker in ("def create_purge_batch", "def cancel_purge_batch", "def retry_purge_batch"):
        start = source.index(marker)
        chunk = source[start:start + 3500]
        i_auth = chunk.index(".authenticate(")
        i_gate = chunk.index("require_ready(")
        i_pipe = chunk.index("pipeline_available(")
        assert i_auth < i_gate < i_pipe
    create = source[source.index("def create_purge_batch"): source.index("def create_purge_batch") + 3500]
    i_auth = create.index(".authenticate(")
    i_key = create.index("INVALID_PURGE_IDEMPOTENCY_KEY")
    i_gate = create.index("require_ready(")
    i_json = create.index("get_json")
    assert i_auth < i_key < i_gate < i_json
    cancel = source[source.index("def cancel_purge_batch"):]
    retry = source[source.index("def retry_purge_batch"):]
    for chunk in (cancel, retry):
        i_auth = chunk.index(".authenticate(")
        i_syntax = chunk.index("INVALID_PURGE_BATCH_ID")
        i_gate = chunk.index("require_ready(")
        assert i_auth < i_syntax < i_gate
    # pipeline_available() 委托无密钥能力源是 #26 对第 4 步的替换。
    # Q9 只允许 create 在安全门之前做 Idempotency-Key 语法检查；cancel/retry 不得绕过 require_ready。


def test_pipeline_available_delegates_to_no_secret_capability_source():
    gate = _read("services/purge_safety_gate.py")
    assert "PURGE_PIPELINE_CAPABILITY_SOURCE" in gate
    assert "UnavailablePurgePipelineCapabilitySource" in gate
    assert "return bool(source.evaluate(" in gate


def test_app_cors_allows_idempotency_key():
    source = _read("app.py")
    assert "Idempotency-Key" in source


def test_control_plane_does_not_touch_backup_or_kodo_or_enable_switches():
    combined = "\n".join(
        _read(path)
        for path in (
            "app.py",
            "blueprints/admin_purge.py",
            "services/admin_auth.py",
            "services/purge_safety_gate.py",
        )
    )
    lowered = combined.lower()
    for forbidden in (
        "BACKUP_OSS_",
        "PURGE_SOURCE_OSS_",
        "PURGE_RESTORE_OSS_",
        ".env.backup",
        "PURGE_ENABLED",
        "PURGE_GATE_NOW",
        "kodo",
    ):
        assert forbidden.lower() not in lowered
    assert "delete_object" not in lowered
    assert "session.delete" not in lowered
