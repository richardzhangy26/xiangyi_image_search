import pytest

from models import AssetActivityRecord
from services.admin_auth import AdminAuth
from services.purge_safety_gate import DictGateEvidenceSource, PurgeSafetyGate

pytestmark = pytest.mark.postgresql


def test_readiness_and_denied_and_rejected_persist(app):
    app.config["ADMIN_AUTH"] = AdminAuth("pg-token", actor_id="admin")
    app.config["PURGE_SAFETY_GATE"] = PurgeSafetyGate(DictGateEvidenceSource({}))
    client = app.test_client()
    denied = client.get("/api/admin/purge/readiness")
    assert denied.status_code == 401
    ok = client.get(
        "/api/admin/purge/readiness",
        headers={"Authorization": "Bearer pg-token"},
    )
    assert ok.status_code == 200
    rejected = client.post(
        "/api/admin/purge/batches",
        headers={"Authorization": "Bearer pg-token"},
    )
    assert rejected.status_code == 409
    assert rejected.get_json()["error_code"] == "PURGE_GATE_NOT_READY"
    records = {row.event_type: row for row in AssetActivityRecord.query.all()}
    assert "purge.auth.denied" in records
    assert records["purge.auth.denied"].after_state == {
        "error_code": records["purge.auth.denied"].error_code
    }
    assert "purge.readiness.read" in records
    assert "purge.batch.create.rejected" in records


def test_unassigned_list_still_works_without_admin(app):
    response = app.test_client().get("/api/image-assets?assignment=unassigned")
    assert response.status_code == 200
