from datetime import datetime, timedelta, timezone
from unittest import mock

from app import create_app
from models import AssetActivityRecord, ImageAsset, db
from services.admin_auth import AdminAuth
from services.purge_safety_gate import (
    CONDITION_IDS,
    DictGateEvidenceSource,
    PurgeSafetyGate,
    RawConditionEvidence,
)

NOW_HEADERS = {"Authorization": "Bearer test-token", "X-Request-ID": "issue-23"}


def _app(source=None, token="test-token"):
    app = create_app("testing")
    with app.app_context():
        db.create_all()
    app.config["ADMIN_AUTH"] = AdminAuth(token, actor_id="admin")
    raw = source if source is not None else {}
    app.config["PURGE_SAFETY_GATE"] = PurgeSafetyGate(DictGateEvidenceSource(raw))
    return app


def _valid_source():
    now = datetime.now(timezone.utc)
    evidence = RawConditionEvidence(
        result="valid",
        verified_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=12),
        summary="ok",
    )
    return {cid: evidence for cid in CONDITION_IDS}


def test_unauthenticated_write_and_get_have_no_readiness():
    app = _app()
    client = app.test_client()
    for method, url in (
        ("get", "/api/admin/purge/readiness"),
        ("post", "/api/admin/purge/batches"),
        ("post", "/api/admin/purge/batches/batch-1/cancel"),
        ("post", "/api/admin/purge/batches/batch-1/retry"),
    ):
        response = getattr(client, method)(url)
        assert response.status_code in (401, 403)
        body = response.get_json()
        assert "readiness" not in body
        assert body["error_code"].startswith("AUTH_")


def test_wrong_token_is_forbidden_without_readiness():
    app = _app()
    response = app.test_client().post(
        "/api/admin/purge/batches",
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 403
    assert response.get_json()["error_code"] == "AUTH_FORBIDDEN"
    assert "readiness" not in response.get_json()


def test_admin_not_ready_get_and_create():
    app = _app({})
    client = app.test_client()
    ready = client.get("/api/admin/purge/readiness", headers=NOW_HEADERS)
    assert ready.status_code == 200
    assert ready.get_json()["purge_available"] is False
    assert ready.get_json()["pipeline_available"] is False
    assert len(ready.get_json()["conditions"]) == 5
    created = client.post("/api/admin/purge/batches", headers=NOW_HEADERS)
    assert created.status_code == 409
    assert created.get_json()["error_code"] == "PURGE_GATE_NOT_READY"
    assert "readiness" in created.get_json()


def test_admin_ready_allow_path_is_pipeline_unavailable():
    app = _app(_valid_source())
    client = app.test_client()
    ready = client.get("/api/admin/purge/readiness", headers=NOW_HEADERS).get_json()
    assert ready["purge_available"] is True
    assert ready["pipeline_available"] is False
    for url in (
        "/api/admin/purge/batches",
        "/api/admin/purge/batches/batch-1/cancel",
        "/api/admin/purge/batches/batch-1/retry",
    ):
        response = client.post(url, headers=NOW_HEADERS)
        assert response.status_code == 409
        assert response.get_json()["error_code"] == "PURGE_PIPELINE_UNAVAILABLE"
        with app.app_context():
            assert ImageAsset.query.count() == 0


def test_auth_denied_after_state_only_error_code():
    app = _app()
    app.test_client().post("/api/admin/purge/batches")
    with app.app_context():
        record = AssetActivityRecord.query.filter_by(
            event_type="purge.auth.denied"
        ).one()
        assert record.after_state == {"error_code": record.error_code}
        assert "purge_available" not in record.after_state
        assert record.actor_id is None
        dumped = str(record.after_state)
        assert "Bearer" not in dumped
        assert "test-token" not in dumped


def test_invalid_batch_id_after_auth():
    app = _app()
    response = app.test_client().post(
        "/api/admin/purge/batches/batch@id/cancel",
        headers=NOW_HEADERS,
    )
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "INVALID_PURGE_BATCH_ID"
    with app.app_context():
        record = AssetActivityRecord.query.filter_by(
            event_type="purge.batch.cancel.rejected"
        ).one()
        assert record.target_id == "invalid"
        assert record.error_code == "INVALID_PURGE_BATCH_ID"


def test_ordinary_asset_list_does_not_need_token():
    app = _app()
    response = app.test_client().get("/api/image-assets?assignment=unassigned")
    assert response.status_code == 200


def test_health_does_not_require_admin():
    app = _app(token=None)
    assert app.test_client().get("/api/health").status_code != 401


def test_audit_failure_returns_500_and_does_not_open_pipeline():
    app = _app(_valid_source())
    with app.app_context():
        with mock.patch.object(
            db.session, "commit", side_effect=RuntimeError("audit boom")
        ):
            response = app.test_client().get(
                "/api/admin/purge/readiness", headers=NOW_HEADERS
            )
    assert response.status_code == 500
    assert response.get_json()["error_code"] == "PURGE_CONTROL_AUDIT_FAILED"


def test_unclassified_failure_returns_500_without_internal_text():
    app = _app({})
    app.config["PURGE_SAFETY_GATE"].evaluate = mock.Mock(
        side_effect=RuntimeError("secret-token-value exploded")
    )
    response = app.test_client().get(
        "/api/admin/purge/readiness", headers=NOW_HEADERS
    )
    assert response.status_code == 500
    body = response.get_json()
    assert body["error_code"] == "PURGE_CONTROL_FAILED"
    assert "secret-token-value" not in body["error"]
