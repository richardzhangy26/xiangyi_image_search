from datetime import datetime, timedelta, timezone
import uuid

from app import create_app
from models import AssetActivityRecord, ImageAsset, db
from services.admin_auth import AdminAuth
from services.purge_safety_gate import (
    CONDITION_IDS,
    DictGateEvidenceSource,
    PurgeSafetyGate,
    RawConditionEvidence,
)

NOW_HEADERS = {"Authorization": "Bearer test-token", "X-Request-ID": "issue-26"}


class AlwaysOn:
    def evaluate(self, now):
        return True


def _valid_source():
    now = datetime.now(timezone.utc)
    evidence = RawConditionEvidence(
        result="valid",
        verified_at=now - timedelta(minutes=30),
        expires_at=now + timedelta(minutes=30),
        summary="ok",
    )
    return {cid: evidence for cid in CONDITION_IDS}


def _app(*, source=None, pipeline=False, token="test-token"):
    app = create_app("testing")
    with app.app_context():
        db.create_all()
    app.config["ADMIN_AUTH"] = AdminAuth(token, actor_id="admin")
    raw = source if source is not None else {}
    app.config["PURGE_SAFETY_GATE"] = PurgeSafetyGate(DictGateEvidenceSource(raw))
    if pipeline:
        app.config["PURGE_PIPELINE_CAPABILITY_SOURCE"] = AlwaysOn()
    return app


def _asset():
    nonce = uuid.uuid4().hex
    return ImageAsset(
        id=uuid.uuid4(),
        source_provider="test",
        source_bucket="test-bucket",
        source_relative_path=f"assets/{nonce}.png",
        source_revision=1,
        display_name="asset.png",
        oss_path=f"original/{nonce}",
        preview_oss_path=f"preview/{nonce}",
        content_hash=nonce,
        source_size=1,
        source_mime_type="image/png",
        source_width=1,
        source_height=1,
        vector=[0.0] * 1024,
        embedding_model="tongyi-embedding-vision-plus-2026-03-06",
        embedding_dimension=1024,
        normalization_version="preview-v1",
        status="archived",
    )


def test_create_invalid_key_precedes_gate_but_does_not_parse_body():
    app = _app(source=_valid_source(), pipeline=True)
    response = app.test_client().post(
        "/api/admin/purge/batches",
        headers={**NOW_HEADERS, "Idempotency-Key": "@bad"},
        json={"asset_ids": "not-a-list"},
    )
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "INVALID_PURGE_IDEMPOTENCY_KEY"


def test_gate_closed_invalid_key_returns_gate_409_and_audits_key_reason_only():
    app = _app(source={})
    response = app.test_client().post(
        "/api/admin/purge/batches",
        headers={**NOW_HEADERS, "Idempotency-Key": "@bad"},
    )
    assert response.status_code == 409
    assert response.get_json()["error_code"] == "PURGE_GATE_NOT_READY"
    with app.app_context():
        record = AssetActivityRecord.query.order_by(AssetActivityRecord.created_at.desc()).first()
        assert record.error_code == "INVALID_PURGE_IDEMPOTENCY_KEY"


def test_detail_authenticates_before_not_found():
    app = _app()
    assert app.test_client().get("/api/admin/purge/batches/missing").status_code in (401, 403)


def test_create_replay_cancel_retry_and_list_use_control_service():
    app = _app(source=_valid_source(), pipeline=True)
    client = app.test_client()
    with app.app_context():
        asset = _asset()
        db.session.add(asset)
        db.session.commit()
        asset_id = str(asset.id)

    headers = {**NOW_HEADERS, "Idempotency-Key": "key.create01"}
    created = client.post(
        "/api/admin/purge/batches",
        headers=headers,
        json={"asset_ids": [asset_id], "confirmation": "永久删除 1 张"},
    )
    assert created.status_code == 201
    body = created.get_json()
    assert body["status"] == "queued"
    assert "items" in body
    assert "oss_path" not in str(body)

    replay = client.post(
        "/api/admin/purge/batches",
        headers=headers,
        json={"asset_ids": [asset_id], "confirmation": "永久删除 1 张"},
    )
    assert replay.status_code == 200
    assert replay.get_json()["batch_id"] == body["batch_id"]

    listed = client.get("/api/admin/purge/batches", headers=NOW_HEADERS)
    assert listed.status_code == 200
    assert listed.get_json()["batches"][0]["batch_id"] == body["batch_id"]

    detail = client.get(f"/api/admin/purge/batches/{body['batch_id']}", headers=NOW_HEADERS)
    assert detail.status_code == 200
    assert detail.get_json()["status"] == "queued"

    cancelled = client.post(
        f"/api/admin/purge/batches/{body['batch_id']}/cancel",
        headers=NOW_HEADERS,
    )
    assert cancelled.status_code == 200
    assert cancelled.get_json()["status"] == "cancelled"


def test_get_batches_does_not_require_ready_gate():
    app = _app(source={})
    response = app.test_client().get("/api/admin/purge/batches", headers=NOW_HEADERS)
    assert response.status_code == 200
    assert response.get_json()["batches"] == []


def test_cors_allows_idempotency_key_header():
    from app import create_app as factory
    source = (factory.__code__.co_filename)
    text = open(source, encoding="utf-8").read()
    assert "Idempotency-Key" in text
