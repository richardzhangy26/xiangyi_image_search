"""Issue #26 的 Compose 证据卷与运维文档静态合同。"""

from pathlib import Path
import re


BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent


def _read_root(relative_path: str) -> str:
    return (ROOT_DIR / relative_path).read_text(encoding="utf-8")


def _service_block(compose: str, service_name: str) -> str:
    marker = f"  {service_name}:"
    start = compose.index(marker)
    next_service = re.search(r"^  [A-Za-z][A-Za-z0-9_-]*:", compose[start + len(marker):], re.MULTILINE)
    if next_service is None:
        return compose[start:]
    end = start + len(marker) + next_service.start()
    return compose[start:end]


def test_compose_separates_gate_and_worker_capability_evidence_volumes():
    compose = _read_root("docker-compose.yml")
    backend = _service_block(compose, "backend")
    image_worker = _service_block(compose, "worker")
    cleanup = _service_block(compose, "cleanup")
    purge_worker = _service_block(compose, "purge-batch-worker")

    assert "PURGE_GATE_EVIDENCE_DIR=/app/purge-gate-evidence" in backend
    assert "target: backend-runtime" in backend
    assert "target: backend-runtime" in image_worker
    assert "target: backend-runtime" in cleanup
    assert "PURGE_GATE_EVIDENCE_DIR=/app/purge-gate-evidence" in purge_worker
    assert "./backend/.env.backup" not in backend
    assert "./backend/.env.backup" in purge_worker
    assert "purge_gate_evidence:/app/purge-gate-evidence:ro" in backend
    assert "purge_gate_evidence:/app/purge-gate-evidence:ro" in purge_worker
    assert "PURGE_PIPELINE_EVIDENCE_DIR=/app/purge-evidence" in backend
    assert "purge_pipeline_evidence:/app/purge-evidence:ro" in backend
    assert "purge_pipeline_evidence:/app/purge-evidence" in purge_worker
    assert "purge_pipeline_evidence:/app/purge-evidence:ro" not in purge_worker
    assert "target: purge-batch-worker-runtime" in purge_worker
    assert "image: fashion-crm-purge-batch-worker:latest" in purge_worker
    assert "BACKUP_ROOT=/var/lib/purge-batch-worker/postgres" in purge_worker
    assert "PURGE_OBJECT_BACKUP_LOCAL_ROOT=/var/lib/purge-batch-worker/object-manifests" in purge_worker
    assert "purge_batch_worker_state:/var/lib/purge-batch-worker" in purge_worker


def test_runbook_locks_evidence_mount_paths_and_ownership_contract():
    runbook = _read_root("docs/operations/purge-batch-pipeline-runbook.md")

    for required in (
        "PURGE_GATE_EVIDENCE_DIR=/app/purge-gate-evidence",
        "PURGE_PIPELINE_EVIDENCE_DIR=/app/purge-evidence",
        "purge_gate_evidence:/app/purge-gate-evidence:ro",
        "purge_pipeline_evidence:/app/purge-evidence:ro",
        "purge_pipeline_evidence:/app/purge-evidence",
        "120 秒",
        "30 秒",
        "worker UID",
        "唯一写入者",
    ):
        assert required in runbook


def test_agents_and_runbook_state_the_current_purge_worker_facts():
    agents = _read_root("AGENTS.md")
    runbook = _read_root("docs/operations/purge-batch-pipeline-runbook.md")
    assert "purge-batch-worker" in agents
    assert "PostgresReferenceSnapshotReader" in agents
    assert "不自动清理" in runbook
    assert "PURGE_BACKUP_RETENTION_EXPIRED" in runbook
    assert "恒为 False" not in agents
    assert "没有 PostgreSQL 引用快照生产 Adapter" not in agents


def test_worker_composition_roots_have_no_delete_or_kodo_capability():
    roots = [
        BACKEND_DIR / "scripts/run_purge_batch_worker.py",
        BACKEND_DIR / "scripts/purge_batch_worker_entrypoint.sh",
        BACKEND_DIR / "services/purge_batch_worker.py",
        BACKEND_DIR / "services/purge_batch_control.py",
        BACKEND_DIR / "services/postgres_reference_snapshot.py",
        BACKEND_DIR / "blueprints/admin_purge.py",
        BACKEND_DIR / "services/asset_recycle_bin.py",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in roots).lower()
    assert "session.delete" not in combined
    assert "delete_object" not in combined
    assert "from services.kodo" not in combined
    assert "import kodo" not in combined
    assert "delete from" not in combined
    assert "oss2.bucket.delete" not in combined


def test_worker_entry_is_the_only_ops_composition_root_and_has_no_delete_calls():
    entry = (BACKEND_DIR / "scripts/run_purge_batch_worker.py").read_text(encoding="utf-8")
    dockerfile = (BACKEND_DIR / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (BACKEND_DIR / "scripts/purge_batch_worker_entrypoint.sh").read_text(encoding="utf-8")
    assert "PurgeObjectBackupService" in entry
    assert "PostgresBackupService" in entry
    assert "OssBackupStorage" in entry
    assert "OssPurgeSourceReader" in entry
    assert "PostgresRestorePointGate" in entry
    assert "write_capability_evidence" in entry
    assert "delete_object" not in entry.lower()
    assert "session.delete" not in entry.lower()
    assert "OssPurgeIsolationStorage" not in entry
    assert "AS purge-batch-worker-runtime" in dockerfile
    assert "postgresql-client-16" in dockerfile
    assert "purge_batch_worker_entrypoint.sh" in dockerfile
    assert "chown -R 1000:1000 /app/purge-evidence /var/lib/purge-batch-worker" in entrypoint
    assert "setpriv --reuid=1000 --regid=1000 --init-groups" in entrypoint
    assert ".env.backup" not in entrypoint
    assert "write_capability" not in entrypoint
    assert "python -m scripts.run_purge_batch_worker" not in entrypoint


def test_backup_env_is_excluded_from_every_docker_build_context():
    ignored = (BACKEND_DIR / '.dockerignore').read_text(encoding='utf-8')
    assert '.env.backup' in ignored
