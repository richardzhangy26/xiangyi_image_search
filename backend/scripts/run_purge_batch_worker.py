"""永久清除批次 worker 入口；唯一 ops 组合根。"""

from __future__ import annotations

import json
import hashlib
import os
import signal
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from services.backup_storage import BackupStorageConfig, OssBackupStorage
from services.postgres_backup import (
    BackupConfigError,
    PostgresBackupService,
    PostgresConnectionConfig,
    SubprocessCommandRunner,
)
from services.postgres_reference_snapshot import PostgresReferenceSnapshotReader
from services.purge_batch_control import PurgeBatchControlService
from services.purge_batch_worker import PostgresRestorePointGate, PurgeBatchWorker
from services.purge_formal_authorization import (
    build_formal_purge_authorization_bundle,
)
from services.purge_object_backup import (
    PurgeObjectBackupConfig,
    PurgeObjectBackupManifest,
    PurgeObjectBackupRequest,
    PurgeObjectBackupService,
)
from services.purge_object_restore import PurgeObjectRestoreConfig, PurgeObjectRestoreService
from services.purge_object_storage import OssPurgeSourceReader, PurgeSourceStorageConfig
from services.purge_pipeline_capability import (
    FilePurgePipelineCapabilitySource,
    write_capability_evidence,
)
from services.purge_safety_gate import FileGateEvidenceSource, PurgeSafetyGate

_stop_requested = False
_halt = threading.Event()


class _UnavailableIsolationStore:
    """校验阶段不装配隔离写凭证。"""


def _request_stop(_signum, _frame):
    global _stop_requested
    _stop_requested = True
    _halt.set()


def _evidence_path(environ=None) -> Path:
    environment = environ or os.environ
    return Path(environment.get('PURGE_PIPELINE_EVIDENCE_DIR', '/app/purge-evidence')) / 'purge_batch_worker.json'


def _stopped(stop_event) -> bool:
    return _stop_requested or _halt.is_set() or (stop_event is not None and stop_event.is_set())


def run_loop(build_worker, *, poll_seconds=2.0, heartbeat_seconds=30, stop_event=None) -> int:
    """先预检再循环；任意配置错误只发布失败能力证明。心跳独立于最长备份调用。"""
    global _stop_requested
    _halt.clear()
    try:
        worker = build_worker()
    except Exception:
        write_capability_evidence(_evidence_path(), now=datetime.now(timezone.utc), result='failed', summary='worker preflight failed')
        return 2

    def publish_health():
        preflight = getattr(worker, 'preflight', None)
        if preflight is not None:
            preflight()
        healthy = getattr(worker, 'capability_healthy', lambda: True)()
        write_capability_evidence(
            _evidence_path(), now=datetime.now(timezone.utc),
            result='valid' if healthy else 'failed',
            summary='worker ready' if healthy else 'worker preflight unhealthy',
        )

    publish_health()

    def heartbeat():
        while not _stopped(stop_event):
            trigger = stop_event if stop_event is not None else _halt
            if trigger.wait(heartbeat_seconds):
                break
            if _stopped(stop_event):
                break
            publish_health()

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        while not _stopped(stop_event):
            if not worker.process_one():
                if _stopped(stop_event):
                    break
                time.sleep(poll_seconds)
        return 0
    finally:
        _halt.set()
        if stop_event is not None:
            stop_event.set()


def _read_database_identity(runner, config):
    result = runner.run(
        [
            'psql',
            '--tuples-only',
            '--no-align',
            '--set=ON_ERROR_STOP=1',
            "--command=SELECT current_database() || '|' || current_setting('server_version_num') || '|' || system_identifier::text FROM pg_control_system()",
        ],
        env=config.process_env(),
        timeout=30,
    )
    if getattr(result, 'returncode', 1) != 0:
        return None
    try:
        database_name, _version, system_identifier = result.stdout.decode('utf-8').strip().split('|', 2)
    except (AttributeError, UnicodeDecodeError, ValueError):
        return None
    if not database_name or not system_identifier:
        return None
    return {'database': database_name, 'system_identifier': system_identifier}


def _identities_match(runner, queue_config, backup_config) -> bool:
    try:
        queue_identity = _read_database_identity(runner, queue_config)
        backup_identity = _read_database_identity(runner, backup_config)
    except Exception:
        return False
    return bool(
        queue_identity
        and backup_identity
        and queue_identity['database'] == backup_identity['database']
        and queue_identity['system_identifier'] == backup_identity['system_identifier']
    )


def _retain_until_from_object_manifest(manifest) -> datetime:
    raw = manifest.retention.get('retain_until') if isinstance(manifest.retention, dict) else None
    if raw is None:
        raise FileNotFoundError('object-copy retention missing')
    parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _build_worker(
    environ=None,
    *,
    backup_storage_factory=None,
    source_reader_factory=None,
    runner_factory=None,
    session=None,
    repository=None,
    identities_match=None,
    safety_ready=None,
    capability_available=None,
    on_identity_failed=None,
    worker_id=None,
    now=None,
):
    environment = dict(environ or os.environ)
    backup_root = environment.get('BACKUP_ROOT')
    object_root = environment.get('PURGE_OBJECT_BACKUP_LOCAL_ROOT')
    if not backup_root:
        raise BackupConfigError('缺少专用数据库配置: BACKUP_ROOT')
    if not object_root:
        raise BackupConfigError('缺少专用数据库配置: PURGE_OBJECT_BACKUP_LOCAL_ROOT')

    storage_config = BackupStorageConfig.from_env(environment)
    source_config = PurgeSourceStorageConfig.from_env(environment)
    backup_db = PostgresConnectionConfig.from_env(environment, prefix='BACKUP_DB_')
    queue_db = PostgresConnectionConfig.from_env(environment, prefix='DB_')
    max_age = int(environment.get('PURGE_REFERENCE_SNAPSHOT_MAX_AGE_SECONDS', '60'))

    storage_factory = backup_storage_factory or OssBackupStorage.from_env
    reader_factory = source_reader_factory or OssPurgeSourceReader.from_env
    command_runner = (runner_factory or SubprocessCommandRunner)()
    backup_store = storage_factory(environment)
    formal_objects = reader_factory(environment)

    postgres_backup = PostgresBackupService(
        runner=command_runner,
        storage=backup_store,
        source=backup_db,
        backup_root=Path(backup_root).expanduser().resolve(),
        remote_bucket=storage_config.bucket_name,
        remote_prefix=storage_config.base_prefix,
    )
    restore_points = PostgresRestorePointGate(postgres_backup, now=now)
    snapshot_reader = PostgresReferenceSnapshotReader(session, max_age_seconds=max_age)
    object_config = PurgeObjectBackupConfig(
        formal_bucket=source_config.bucket_name,
        backup_bucket=storage_config.bucket_name,
        backup_prefix=storage_config.base_prefix,
        local_root=Path(object_root).expanduser().resolve(),
        reference_snapshot_max_age_seconds=max_age,
    )
    object_backup = PurgeObjectBackupService(
        restore_points=restore_points,
        references=snapshot_reader,
        formal_objects=formal_objects,
        backup_store=backup_store,
        config=object_config,
        now=now or (lambda: datetime.now(timezone.utc)),
    )
    object_restore = PurgeObjectRestoreService(
        backup_store=backup_store,
        isolated_store=_UnavailableIsolationStore(),
        config=PurgeObjectRestoreConfig(
            formal_bucket=source_config.bucket_name,
            backup_bucket=storage_config.bucket_name,
            backup_prefix=storage_config.base_prefix,
            isolated_bucket='verification-not-used.invalid',
            isolated_prefix='verification-not-used',
            isolated_environment=False,
            temporary_root=Path(object_root).expanduser().resolve() / '.verify',
        ),
    )

    if repository is None and session is None:
        raise BackupConfigError('缺少 worker 数据库会话')
    repo = repository if repository is not None else PurgeBatchControlService(session)

    def load_asset_ids(batch_id):
        loader = getattr(repo, 'item_asset_ids', None)
        if loader is None:
            return ()
        return tuple(str(value) for value in loader(batch_id))

    def handle_database_backup(claim):
        manifest = restore_points.require_verified(str(claim.batch_id))
        return 'object_backup', {
            'database_backup_id': manifest.backup_id,
            'database_manifest_sha256': manifest.artifact_sha256,
            'retain_until': manifest.retain_until,
        }

    def handle_object_backup(claim):
        result = object_backup.create_verified(PurgeObjectBackupRequest(
            purge_batch_id=str(claim.batch_id),
            asset_ids=load_asset_ids(claim.batch_id),
        ))
        return 'verifying', {
            'object_manifest_sha256': result.manifest_sha256,
            'retain_until': _retain_until_from_object_manifest(result.manifest),
        }

    def handle_verifying(claim):
        backup_id = f'purge-{claim.batch_id}'
        local_manifest = Path(object_root).expanduser().resolve() / backup_id / 'objects' / 'manifest.json'
        if not local_manifest.is_file():
            raise FileNotFoundError('object-copy manifest missing')
        manifest_bytes = local_manifest.read_bytes()
        manifest = PurgeObjectBackupManifest.from_dict(json.loads(manifest_bytes))
        if _retain_until_from_object_manifest(manifest) <= datetime.now(timezone.utc):
            raise FileNotFoundError('object-copy retention expired')
        object_restore.verify_copies(manifest)
        object_backup.revalidate_current_candidates(manifest)
        bundle = build_formal_purge_authorization_bundle(
            manifest,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            now=datetime.now(timezone.utc),
        )
        if str(bundle.purge_batch_id) != str(claim.batch_id):
            raise ValueError('object manifest batch mismatch')
        repo.advance_verified_to_pending_if_current(bundle)
        return None

    gate_dir = environment.get('PURGE_GATE_EVIDENCE_DIR')
    gate = PurgeSafetyGate(FileGateEvidenceSource(Path(gate_dir) if gate_dir else None))
    capability_source = FilePurgePipelineCapabilitySource(_evidence_path(environment))

    def write_failed_identity():
        write_capability_evidence(
            _evidence_path(environment),
            now=datetime.now(timezone.utc),
            result='failed',
            summary='database identity mismatch',
        )

    worker = PurgeBatchWorker(
        repo,
        worker_id=worker_id or environment.get(
            'PURGE_BATCH_WORKER_ID',
            f'{socket.gethostname()}-{os.getpid()}',
        ),
        safety_ready=safety_ready or (lambda: gate.evaluate().ready),
        capability_available=capability_available or (
            lambda: capability_source.evaluate(datetime.now(timezone.utc))
        ),
        phase_handlers={
            'database_backup': handle_database_backup,
            'object_backup': handle_object_backup,
            'verifying': handle_verifying,
        },
        identities_match=identities_match or (
            lambda: _identities_match(command_runner, queue_db, backup_db)
        ),
        on_identity_failed=on_identity_failed or write_failed_identity,
    )
    worker.postgres_backup = postgres_backup
    worker.restore_points = restore_points
    worker.object_backup = object_backup
    worker.object_restore = object_restore
    worker.isolated_store = None
    return worker


def main() -> int:
    from dotenv import load_dotenv

    from app import create_app
    from models import db

    load_dotenv(Path(__file__).resolve().parents[1] / '.env.backup')
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    app = create_app()
    with app.app_context():
        return run_loop(
            lambda: _build_worker(session=db.session),
            poll_seconds=float(os.getenv('PURGE_BATCH_POLL_SECONDS', '2')),
        )


if __name__ == '__main__':
    raise SystemExit(main())
