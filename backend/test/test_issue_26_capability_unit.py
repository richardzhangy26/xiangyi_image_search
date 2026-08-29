import json
from datetime import datetime, timedelta, timezone

from flask import Flask


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def test_capability_evidence_is_valid_only_before_120_second_expiry(tmp_path):
    from services.purge_pipeline_capability import (
        FilePurgePipelineCapabilitySource,
        write_capability_evidence,
    )

    path = tmp_path / 'purge_batch_worker.json'
    write_capability_evidence(path, now=NOW, result='valid')
    source = FilePurgePipelineCapabilitySource(path)
    assert source.evaluate(NOW + timedelta(seconds=119)) is True
    assert source.evaluate(NOW + timedelta(seconds=120)) is False


def test_sensitive_nested_key_or_unexpected_key_fails_closed(tmp_path):
    from services.purge_pipeline_capability import FilePurgePipelineCapabilitySource

    path = tmp_path / 'purge_batch_worker.json'
    path.write_text(json.dumps({
        'schema_version': 1, 'component': 'purge_batch_worker', 'result': 'valid',
        'verified_at': NOW.isoformat(), 'expires_at': (NOW + timedelta(seconds=120)).isoformat(),
        'policy': 'backup_only_no_delete', 'summary': {'nested_token': 'forbidden'},
    }), encoding='utf-8')
    assert FilePurgePipelineCapabilitySource(path).evaluate(NOW) is False


def test_pipeline_available_delegates_to_app_source_with_unavailable_fallback():
    from services.purge_safety_gate import pipeline_available

    class Available:
        def evaluate(self, now):
            return True

    app = Flask(__name__)
    with app.app_context():
        assert pipeline_available() is False
        app.config['PURGE_PIPELINE_CAPABILITY_SOURCE'] = Available()
        assert pipeline_available() is True
