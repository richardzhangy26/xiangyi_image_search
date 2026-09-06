from services.purge_formal_deletion_capability import (
    UnavailableFormalDeletionCapabilitySource,
)


def test_one_shot_root_exits_before_constructing_worker_when_capability_is_off():
    from scripts.run_formal_purge_worker import run_one_shot

    constructed = []
    result = run_one_shot(
        worker_factory=lambda: constructed.append("worker"),
        capability=UnavailableFormalDeletionCapabilitySource(),
    )

    assert result == 2
    assert constructed == []


def test_one_shot_exits_disabled_without_worker_when_deployed_without_fence(
    monkeypatch,
):
    from scripts.run_formal_purge_worker import run_one_shot

    monkeypatch.setenv("PURGE_FORMAL_DELETION_DEPLOYED", "1")
    monkeypatch.setenv("INGEST_BINDING_FENCE_ENABLED", "0")
    constructed = []

    class Capability:
        def evaluate(self, received):
            return True

    class Worker:
        def process_one_item(self):
            constructed.append("worker")

    result = run_one_shot(
        worker_factory=lambda: Worker(),
        capability=Capability(),
    )

    assert result == 2
    assert constructed == []


def test_one_shot_exits_disabled_when_context_inventory_digest_mismatches(
    monkeypatch,
):
    from types import SimpleNamespace

    from scripts.run_formal_purge_worker import run_one_shot

    monkeypatch.setenv("PURGE_FORMAL_DELETION_DEPLOYED", "1")
    monkeypatch.setenv("INGEST_BINDING_FENCE_ENABLED", "1")
    constructed = []

    class Capability:
        def evaluate(self, received):
            return True

    class Worker:
        def process_one_item(self):
            constructed.append("worker")

    result = run_one_shot(
        worker_factory=lambda: Worker(),
        capability=Capability(),
        context=SimpleNamespace(writer_inventory_sha256="0" * 64),
    )

    assert result == 2
    assert constructed == []


def test_compose_formal_worker_is_profiled_off_and_names_no_delete_secret():
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    service = compose.split("  formal-purge-worker:", 1)[1].split("\n  frontend:", 1)[0]
    assert 'profiles: ["formal-delete"]' in service
    assert 'command: ["python", "-m", "scripts.run_formal_purge_worker"]' in service
    assert 'restart: "no"' in service
    assert "PURGE_FORMAL_DELETION_ENABLED=0" in service
    assert "PURGE_FORMAL_DELETION_ENABLED=1" not in service
    assert "PURGE_DELETE_OSS_" not in service
    assert ".env.backup" not in service
    assert ".env.formal" not in service
    assert "env_file:" not in service
    assert "unless-stopped" not in service
    assert "healthcheck:" in service
    assert "disable: true" in service
    healthcheck = service.split("healthcheck:", 1)[1]
    assert "disable: true" in healthcheck.split("\n    networks:", 1)[0]
    if "test:" in healthcheck.split("\n    networks:", 1)[0]:
        probe = healthcheck.split("\n    networks:", 1)[0]
        assert "scripts.check_formal_purge_health" in probe
        assert "PURGE_DELETE_OSS_" not in probe


def test_main_composes_unavailable_capability_and_names_no_delete_adapter():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_formal_purge_worker.py"
    ).read_text(encoding="utf-8")
    assert "capability=UnavailableFormalDeletionCapabilitySource()" in source
    assert "FormalPurgeRepository" not in source
    assert "OssFormalObjectDeleter" not in source
    assert "FileFormalDeletionCapabilitySource" not in source
    assert "PURGE_DELETE_OSS_" not in source


def test_formal_health_cli_fails_closed_when_evidence_missing(tmp_path):
    from io import StringIO

    from scripts.check_formal_purge_health import main as health_main

    output = StringIO()
    missing = tmp_path / "absent-formal-health.json"
    assert health_main(["--evidence", str(missing)], stdout=output) == 2


def test_one_shot_passes_exact_context_to_capability_before_worker_factory():
    from scripts.run_formal_purge_worker import run_one_shot

    context = object()
    calls = []

    class Capability:
        def evaluate(self, received):
            calls.append(("capability", received))
            return object()

    class Worker:
        def process_one_item(self):
            calls.append(("worker", context))

    result = run_one_shot(
        worker_factory=lambda: Worker(),
        capability=Capability(),
        context=context,
    )

    assert result == 0
    assert calls == [("capability", context), ("worker", context)]


def test_one_shot_disabled_publishes_health_and_safe_event(tmp_path):
    import json
    import uuid
    from types import SimpleNamespace

    from scripts.run_formal_purge_worker import run_one_shot
    from services.formal_purge_observability import JsonFormalPurgeEventSink

    batch_id = uuid.uuid4()
    context = SimpleNamespace(
        environment_id='prod-cn-shanghai-primary', batch_id=batch_id,
    )
    health_path = tmp_path / 'formal-health.json'
    events = []
    result = run_one_shot(
        worker_factory=lambda: (_ for _ in ()).throw(
            AssertionError('disabled root must not construct worker')
        ),
        capability=UnavailableFormalDeletionCapabilitySource(),
        context=context,
        health_path=health_path,
        event_sink=JsonFormalPurgeEventSink(events.append),
    )

    assert result == 2
    health = json.loads(health_path.read_text(encoding='utf-8'))
    assert health['result'] == 'disabled'
    assert health['batch_id'] == str(batch_id)
    assert 'summary' not in health
    event = json.loads(events[0])
    assert event['event_type'] == 'purge.formal.capability.denied'
    assert event['error_code'] == 'PURGE_FORMAL_DELETION_DISABLED'
