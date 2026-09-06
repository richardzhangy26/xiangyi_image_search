import json
import uuid
from datetime import datetime, timedelta, timezone
from io import StringIO


def test_health_evidence_is_typed_short_lived_and_fails_closed_after_expiry(tmp_path):
    from services.formal_purge_observability import (
        FileFormalPurgeHealthSource,
        write_formal_purge_health,
    )

    now = datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc)
    path = tmp_path / "formal-health.json"
    batch_id = uuid.uuid4()
    write_formal_purge_health(
        path,
        now=now,
        result="valid",
        environment_id="prod-cn-shanghai-primary",
        batch_id=batch_id,
        stage="preflight",
        error_code=None,
    )
    source = FileFormalPurgeHealthSource(path)

    snapshot = source.evaluate(now=now + timedelta(seconds=30))

    assert snapshot.available is True
    assert snapshot.batch_id == batch_id
    assert snapshot.stage == "preflight"
    assert source.evaluate(now=now + timedelta(seconds=121)).available is False


def test_structured_event_sink_emits_only_safe_typed_fields():
    from services.formal_purge_observability import (
        FormalPurgeOperationalEvent,
        JsonFormalPurgeEventSink,
    )

    messages = []
    event = FormalPurgeOperationalEvent(
        event_type="purge.formal.capability.denied",
        occurred_at=datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc),
        environment_id="prod-cn-shanghai-primary",
        batch_id=uuid.uuid4(),
        target_asset_id=uuid.uuid4(),
        checkpoint="original_delete_started",
        result="denied",
        error_code="PURGE_FORMAL_DELETION_DISABLED",
    )
    JsonFormalPurgeEventSink(messages.append).emit(event)

    payload = json.loads(messages[0])
    assert set(payload) == {
        "schema_version", "event_type", "occurred_at", "environment_id",
        "batch_id", "target_asset_id", "checkpoint", "result", "error_code",
    }
    assert "key" not in messages[0].lower()
    assert "secret" not in messages[0].lower()


def test_health_check_cli_is_read_only_and_returns_stable_exit_codes(tmp_path):
    from scripts.check_formal_purge_health import main
    from services.formal_purge_observability import write_formal_purge_health

    now = datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc)
    path = tmp_path / "formal-health.json"
    write_formal_purge_health(
        path,
        now=now,
        result="valid",
        environment_id="prod-cn-shanghai-primary",
        batch_id=uuid.uuid4(),
        stage="preflight",
        error_code=None,
    )
    output = StringIO()
    assert main(
        ["--evidence", str(path)],
        now=now + timedelta(seconds=30),
        stdout=output,
    ) == 0
    assert json.loads(output.getvalue())["available"] is True

    expired = StringIO()
    assert main(
        ["--evidence", str(path)],
        now=now + timedelta(seconds=121),
        stdout=expired,
    ) == 2
    assert json.loads(expired.getvalue()) == {
        "available": False,
        "error_code": "PURGE_FORMAL_HEALTH_UNAVAILABLE",
    }
