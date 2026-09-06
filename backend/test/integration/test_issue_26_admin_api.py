"""Issue #26 管理 API 的真实 PostgreSQL 合同；无库时 skip。"""

import pytest

from services.admin_auth import AdminAuth
from services.purge_safety_gate import DictGateEvidenceSource, PurgeSafetyGate

pytestmark = pytest.mark.postgresql


def test_list_batches_is_observable_when_gate_is_closed(app):
    app.config['ADMIN_AUTH'] = AdminAuth('pg-token', actor_id='admin')
    app.config['PURGE_SAFETY_GATE'] = PurgeSafetyGate(DictGateEvidenceSource({}))
    client = app.test_client()
    denied = client.get('/api/admin/purge/batches')
    assert denied.status_code in (401, 403)
    listed = client.get(
        '/api/admin/purge/batches',
        headers={'Authorization': 'Bearer pg-token'},
    )
    assert listed.status_code == 200
    assert listed.get_json()['batches'] == []
