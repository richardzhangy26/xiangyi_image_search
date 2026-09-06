from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.purge_safety_gate import (
    CONDITION_IDS,
    MAX_EVIDENCE_FILE_BYTES,
    DictGateEvidenceSource,
    FileGateEvidenceSource,
    GateNotReady,
    PurgeSafetyGate,
    RawConditionEvidence,
    pipeline_available,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def _valid_raw(**overrides):
    payload = dict(
        result="valid",
        verified_at=NOW - timedelta(minutes=30),
        expires_at=NOW + timedelta(minutes=30),
        summary="ok",
        parse_error=False,
    )
    payload.update(overrides)
    return RawConditionEvidence(**payload)


def _all_valid():
    return {cid: _valid_raw() for cid in CONDITION_IDS}


def _file_payload(condition, **overrides):
    payload = {
        "schema_version": 1,
        "condition": condition,
        "result": "valid",
        "verified_at": "2026-08-22T04:00:00Z",
        "expires_at": "2026-08-23T05:00:00Z",
        "summary": "ok",
    }
    payload.update(overrides)
    return payload


def test_pipeline_available_is_false_without_flask_capability_source():
    from flask import Flask

    with Flask(__name__).app_context():
        assert pipeline_available() is False


def test_missing_dir_is_all_unknown():
    snap = PurgeSafetyGate(FileGateEvidenceSource(None), clock=lambda: NOW).evaluate()
    assert snap.ready is False
    assert [c.status for c in snap.conditions] == ["unknown"] * 5
    assert [c.id for c in snap.conditions] == list(CONDITION_IDS)


def test_empty_dir_is_all_unknown(tmp_path: Path):
    snap = PurgeSafetyGate(FileGateEvidenceSource(tmp_path), clock=lambda: NOW).evaluate()
    assert snap.ready is False
    assert [c.status for c in snap.conditions] == ["unknown"] * 5


def test_load_exception_marks_all_conditions_unknown():
    class Boom:
        def load(self, now):
            raise RuntimeError("disk failed")

    snap = PurgeSafetyGate(Boom(), clock=lambda: NOW).evaluate()
    assert snap.ready is False
    assert [c.status for c in snap.conditions] == ["unknown"] * 5


def test_expired_and_future_verified_at():
    source = DictGateEvidenceSource({
        **_all_valid(),
        "recovery_drill": _valid_raw(expires_at=NOW - timedelta(seconds=1)),
    })
    snap = PurgeSafetyGate(source, clock=lambda: NOW).evaluate()
    assert snap.ready is False
    assert {c.id: c.status for c in snap.conditions}["recovery_drill"] == "expired"

    source = DictGateEvidenceSource({
        **_all_valid(),
        "object_protection": _valid_raw(
            verified_at=NOW + timedelta(seconds=120),
        ),
    })
    snap = PurgeSafetyGate(source, clock=lambda: NOW).evaluate()
    assert {c.id: c.status for c in snap.conditions}["object_protection"] == "unknown"


def test_condition_specific_max_freshness_cannot_be_extended_by_publisher():
    too_old = DictGateEvidenceSource({
        **_all_valid(),
        "instant_restore_point_capability": _valid_raw(
            verified_at=NOW - timedelta(minutes=61),
            expires_at=NOW + timedelta(minutes=1),
        ),
    })
    statuses = {
        item.id: item.status
        for item in PurgeSafetyGate(too_old, clock=lambda: NOW).evaluate().conditions
    }
    assert statuses["instant_restore_point_capability"] == "expired"

    too_long = DictGateEvidenceSource({
        **_all_valid(),
        "object_protection": _valid_raw(
            verified_at=NOW,
            expires_at=NOW + timedelta(hours=25),
        ),
    })
    statuses = {
        item.id: item.status
        for item in PurgeSafetyGate(too_long, clock=lambda: NOW).evaluate().conditions
    }
    assert statuses["object_protection"] == "failed"


def test_nested_secret_key_is_parse_error(tmp_path: Path):
    import json
    (tmp_path / "daily_postgres_backup.json").write_text(
        json.dumps(_file_payload(
            "daily_postgres_backup",
            meta={"token": "leak"},
        )),
        encoding="utf-8",
    )
    snap = PurgeSafetyGate(
        FileGateEvidenceSource(tmp_path), clock=lambda: NOW
    ).evaluate()
    assert snap.conditions[0].status == "failed"


def test_oversized_file_is_parse_error(tmp_path: Path):
    payload = (
        '{"schema_version":1,"condition":"daily_postgres_backup",'
        '"result":"valid","verified_at":"2026-08-22T04:00:00Z",'
        '"expires_at":"2026-08-23T05:00:00Z","summary":"'
        + ("x" * MAX_EVIDENCE_FILE_BYTES)
        + '"}'
    )
    (tmp_path / "daily_postgres_backup.json").write_text(payload, encoding="utf-8")
    snap = PurgeSafetyGate(
        FileGateEvidenceSource(tmp_path), clock=lambda: NOW
    ).evaluate()
    assert snap.conditions[0].status == "failed"


def test_five_valid_ready_and_require_ready():
    gate = PurgeSafetyGate(DictGateEvidenceSource(_all_valid()), clock=lambda: NOW)
    snap = gate.require_ready()
    assert snap.ready is True


def test_require_ready_raises_when_not_ready():
    gate = PurgeSafetyGate(DictGateEvidenceSource({}), clock=lambda: NOW)
    with pytest.raises(GateNotReady) as exc:
        gate.require_ready()
    assert exc.value.error_code == "PURGE_GATE_NOT_READY"
    assert exc.value.snapshot.ready is False


def test_bad_json_and_wrong_schema_and_condition_mismatch(tmp_path: Path):
    import json
    (tmp_path / "daily_postgres_backup.json").write_text("not-json", encoding="utf-8")
    (tmp_path / "instant_restore_point_capability.json").write_text(
        json.dumps(_file_payload("instant_restore_point_capability", schema_version=2)),
        encoding="utf-8",
    )
    (tmp_path / "object_protection.json").write_text(
        json.dumps(_file_payload("daily_postgres_backup")),
        encoding="utf-8",
    )
    snap = PurgeSafetyGate(
        FileGateEvidenceSource(tmp_path), clock=lambda: NOW
    ).evaluate()
    statuses = {item.id: item.status for item in snap.conditions}
    assert statuses["daily_postgres_backup"] == "failed"
    assert statuses["instant_restore_point_capability"] == "failed"
    assert statuses["object_protection"] == "failed"


def test_document_expired_result_is_failed_not_clock_bypass():
    source = DictGateEvidenceSource({
        **_all_valid(),
        "recovery_drill": _valid_raw(result="expired"),
    })
    snap = PurgeSafetyGate(source, clock=lambda: NOW).evaluate()
    assert {c.id: c.status for c in snap.conditions}["recovery_drill"] == "failed"
    assert snap.ready is False


def test_file_source_only_reads_known_condition_files(tmp_path: Path):
    import json
    (tmp_path / "../evil.json").write_text("{}", encoding="utf-8")
    (tmp_path / "not-a-condition.json").write_text(
        json.dumps(_file_payload("daily_postgres_backup")),
        encoding="utf-8",
    )
    snap = PurgeSafetyGate(
        FileGateEvidenceSource(tmp_path), clock=lambda: NOW
    ).evaluate()
    assert [c.status for c in snap.conditions] == ["unknown"] * 5
