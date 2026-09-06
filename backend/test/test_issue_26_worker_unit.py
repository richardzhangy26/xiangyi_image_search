from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest


@dataclass(frozen=True)
class Claim:
    batch_id: str = 'batch-1'
    claim_token: str = 'token-1'
    claim_generation: int = 1
    expected_status: str = 'database_backup'


def _ops_environ(tmp_path: Path) -> dict[str, str]:
    return {
        'BACKUP_ROOT': str(tmp_path / 'postgres'),
        'PURGE_OBJECT_BACKUP_LOCAL_ROOT': str(tmp_path / 'objects'),
        'BACKUP_DB_HOST': 'db.internal',
        'BACKUP_DB_PORT': '5432',
        'BACKUP_DB_NAME': 'image_search',
        'BACKUP_DB_USER': 'backup_reader',
        'BACKUP_DB_PASSWORD': 'backup-secret',
        'DB_HOST': 'db.internal',
        'DB_PORT': '5432',
        'DB_NAME': 'image_search',
        'DB_USER': 'app',
        'DB_PASSWORD': 'app-secret',
        'BACKUP_OSS_ACCESS_KEY_ID': 'backup-key',
        'BACKUP_OSS_ACCESS_KEY_SECRET': 'backup-secret-oss',
        'BACKUP_OSS_ENDPOINT': 'oss-cn-shanghai.aliyuncs.com',
        'BACKUP_OSS_BUCKET_NAME': 'backup-bucket',
        'BACKUP_OSS_BASE_PREFIX': 'postgresql-backups',
        'PURGE_SOURCE_OSS_ACCESS_KEY_ID': 'source-key',
        'PURGE_SOURCE_OSS_ACCESS_KEY_SECRET': 'source-secret',
        'PURGE_SOURCE_OSS_ENDPOINT': 'oss-cn-shanghai.aliyuncs.com',
        'PURGE_SOURCE_OSS_BUCKET_NAME': 'formal-bucket',
        'OSS_ACCESS_KEY_ID': 'app-oss-key',
        'OSS_BUCKET_NAME': 'app-bucket',
        'PURGE_REFERENCE_SNAPSHOT_MAX_AGE_SECONDS': '60',
        'PURGE_PIPELINE_EVIDENCE_DIR': str(tmp_path / 'evidence'),
        'PURGE_GATE_EVIDENCE_DIR': str(tmp_path / 'gate'),
    }


class FakeStore:
    def head(self, key):
        return None

    def put_file_if_absent(self, *args, **kwargs):
        return None

    def put_bytes_if_absent(self, *args, **kwargs):
        return None

    def download_to(self, *args, **kwargs):
        return None


class FakeRepo:
    def __init__(self):
        self.claimed = False
        self.advanced = None
        self.error = None
        self.status = 'database_backup'
        self.promoted_bundle = None

    def claim_next(self, **kwargs):
        self.claimed = True
        return Claim()

    def advance_if_current(self, claim, *, status, **kwargs):
        self.advanced = {'status': status, **kwargs}
        return self.status != 'cancelled'

    def fail_if_current(self, claim, *, error_code, **kwargs):
        self.error = error_code
        return True

    def record_stale_result(self, claim, **kwargs):
        return None

    def advance_verified_to_pending_if_current(self, bundle):
        self.promoted_bundle = bundle
        return True


def test_late_result_after_cancel_never_advances_batch():
    from services.purge_batch_worker import PurgeBatchWorker

    repo = FakeRepo()
    repo.status = 'cancelled'
    worker = PurgeBatchWorker(
        repo, worker_id='w1', safety_ready=lambda: True, capability_available=lambda: True,
        phase_handlers={'database_backup': lambda claim: 'object_backup'},
    )
    assert worker.process_one() is True
    assert repo.advanced['status'] == 'object_backup'
    assert repo.status == 'cancelled'


def test_retention_expiry_fails_through_cas_without_external_delete():
    from services.purge_batch_worker import PurgeBatchWorker

    repo = FakeRepo()
    worker = PurgeBatchWorker(
        repo, worker_id='w1', safety_ready=lambda: True, capability_available=lambda: True,
        phase_handlers={'database_backup': lambda claim: (_ for _ in ()).throw(FileNotFoundError())},
    )
    assert worker.process_one() is True
    assert repo.error == 'PURGE_BACKUP_RETENTION_EXPIRED'


def test_worker_loop_heartbeats_and_fails_closed_when_preflight_rejects(monkeypatch, tmp_path):
    from scripts import run_purge_batch_worker as entry

    writes = []
    monkeypatch.setattr(entry, 'write_capability_evidence', lambda *args, **kwargs: writes.append(kwargs['result']))
    monkeypatch.setattr(entry, '_evidence_path', lambda: tmp_path / 'capability.json')
    assert entry.run_loop(lambda: (_ for _ in ()).throw(RuntimeError('bad config')), poll_seconds=0) == 2
    assert writes == ['failed']


def test_build_worker_fails_closed_without_backup_roots_and_does_not_construct_storage(tmp_path):
    from scripts.run_purge_batch_worker import _build_worker
    from services.postgres_backup import BackupConfigError

    environ = _ops_environ(tmp_path)
    del environ['BACKUP_ROOT']
    constructed = []

    with pytest.raises(BackupConfigError):
        _build_worker(
            environ,
            backup_storage_factory=lambda env: constructed.append('backup') or FakeStore(),
            source_reader_factory=lambda env: constructed.append('source') or FakeStore(),
            repository=FakeRepo(),
        )
    assert constructed == []


def test_build_worker_composes_ops_adapters_and_phase_handlers_from_injected_factories(tmp_path):
    from scripts.run_purge_batch_worker import _build_worker
    from services.postgres_backup import PostgresBackupService
    from services.purge_batch_worker import PostgresRestorePointGate, PurgeBatchWorker
    from services.purge_object_backup import PurgeObjectBackupService
    from services.purge_object_restore import PurgeObjectRestoreService

    environ = _ops_environ(tmp_path)
    seen = []
    backup_store = FakeStore()
    source_store = FakeStore()

    worker = _build_worker(
        environ,
        backup_storage_factory=lambda env: seen.append(('backup', env['BACKUP_OSS_BUCKET_NAME'])) or backup_store,
        source_reader_factory=lambda env: seen.append(('source', env['PURGE_SOURCE_OSS_BUCKET_NAME'])) or source_store,
        repository=FakeRepo(),
        identities_match=lambda: True,
    )

    assert isinstance(worker, PurgeBatchWorker)
    assert isinstance(worker.postgres_backup, PostgresBackupService)
    assert isinstance(worker.restore_points, PostgresRestorePointGate)
    assert isinstance(worker.object_backup, PurgeObjectBackupService)
    assert isinstance(worker.object_restore, PurgeObjectRestoreService)
    assert worker.object_backup.config.reference_snapshot_max_age_seconds == 60
    assert worker.object_backup.config.formal_bucket == 'formal-bucket'
    assert worker.object_backup.config.backup_bucket == 'backup-bucket'
    assert worker.object_backup.formal_objects is source_store
    assert worker.object_backup.backup_store is backup_store
    assert set(worker.phase_handlers) == {'database_backup', 'object_backup', 'verifying'}
    assert seen == [('backup', 'backup-bucket'), ('source', 'formal-bucket')]
    assert not hasattr(worker, 'isolated_store') or worker.isolated_store is None


def test_verifying_phase_promotes_typed_bundle_without_generic_status_advance(
    monkeypatch, tmp_path,
):
    from scripts import run_purge_batch_worker as entry

    environ = _ops_environ(tmp_path)
    repository = FakeRepo()
    worker = entry._build_worker(
        environ,
        backup_storage_factory=lambda _env: FakeStore(),
        source_reader_factory=lambda _env: FakeStore(),
        repository=repository,
        identities_match=lambda: True,
    )
    manifest_path = (
        Path(environ['PURGE_OBJECT_BACKUP_LOCAL_ROOT'])
        / 'purge-batch-1'
        / 'objects'
        / 'manifest.json'
    )
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text('{}', encoding='utf-8')
    manifest = SimpleNamespace(
        retention={'retain_until': '2099-01-01T00:00:00Z'},
    )
    bundle = SimpleNamespace(purge_batch_id='batch-1')
    monkeypatch.setattr(
        entry.PurgeObjectBackupManifest,
        'from_dict',
        classmethod(lambda _cls, _payload: manifest),
    )
    monkeypatch.setattr(
        entry,
        'build_formal_purge_authorization_bundle',
        lambda parsed, **_kwargs: bundle,
        raising=False,
    )
    monkeypatch.setattr(worker.object_restore, 'verify_copies', lambda _manifest: None)
    monkeypatch.setattr(
        worker.object_backup,
        'revalidate_current_candidates',
        lambda _manifest: None,
    )

    outcome = worker.phase_handlers['verifying'](
        Claim(expected_status='verifying')
    )

    assert outcome is None
    assert repository.promoted_bundle is bundle
    assert repository.advanced is None


def test_worker_refuses_to_claim_when_queue_and_backup_database_identities_differ():
    from services.purge_batch_worker import PurgeBatchWorker

    repo = FakeRepo()
    capability = SimpleNamespace(last_result=None)

    def mark_failed():
        capability.last_result = 'failed'

    worker = PurgeBatchWorker(
        repo,
        worker_id='w1',
        safety_ready=lambda: True,
        capability_available=lambda: True,
        identities_match=lambda: False,
        on_identity_failed=mark_failed,
    )
    assert worker.process_one() is False
    assert capability.last_result == 'failed'
    assert repo.claimed is False


def test_restore_point_gate_creates_then_verifies_and_rejects_expired_retention(tmp_path):
    from services.purge_batch_worker import PostgresRestorePointGate

    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    calls = []

    class Backup:
        backup_root = tmp_path

        def __init__(self, retain_until):
            self.retain_until = retain_until

        def create_backup(self, request):
            calls.append(('create', request.backup_id, request.kind, request.purge_batch_id))
            directory = tmp_path / request.backup_id
            directory.mkdir(exist_ok=True)
            (directory / 'manifest.json').write_text('{}', encoding='utf-8')
            return SimpleNamespace(manifest=SimpleNamespace(
                backup_id=request.backup_id,
                retain_until=self.retain_until,
            ))

        def verify_copies(self, path):
            calls.append(('verify', Path(path).name))
            return SimpleNamespace(status='verified')

    gate = PostgresRestorePointGate(Backup(now + timedelta(days=30)), now=lambda: now)
    manifest = gate.require_verified('batch-001')
    assert [item[0] for item in calls] == ['create', 'verify']
    assert calls[0][1:] == ('purge-batch-001', 'purge_restore_point', 'batch-001')
    assert manifest.backup_id == 'purge-batch-001'

    calls.clear()
    expired = PostgresRestorePointGate(Backup(now - timedelta(seconds=1)), now=lambda: now)
    with pytest.raises(FileNotFoundError):
        expired.require_verified('batch-001')


def test_phase_handlers_advance_with_backup_evidence_and_map_snapshot_errors():
    from services.purge_batch_worker import PurgeBatchWorker
    from services.purge_object_backup import PurgeObjectReferenceError

    repo = FakeRepo()
    worker = PurgeBatchWorker(
        repo, worker_id='w1', safety_ready=lambda: True, capability_available=lambda: True,
        phase_handlers={
            'database_backup': lambda claim: (
                'object_backup',
                {
                    'database_backup_id': 'purge-batch-1',
                    'database_manifest_sha256': 'a' * 64,
                },
            ),
        },
    )
    assert worker.process_one() is True
    assert repo.advanced['status'] == 'object_backup'
    assert repo.advanced['database_backup_id'] == 'purge-batch-1'

    repo = FakeRepo()
    worker = PurgeBatchWorker(
        repo, worker_id='w1', safety_ready=lambda: True, capability_available=lambda: True,
        phase_handlers={
            'database_backup': lambda claim: (_ for _ in ()).throw(
                PurgeObjectReferenceError('实时引用快照')
            ),
        },
    )
    assert worker.process_one() is True
    assert repo.error == 'PURGE_REFERENCE_SNAPSHOT_INVALID'


def test_heartbeat_continues_during_long_process_one(monkeypatch, tmp_path):
    from scripts import run_purge_batch_worker as entry

    writes = []
    started = threading.Event()
    stop = threading.Event()

    class SlowWorker:
        def process_one(self):
            started.set()
            time.sleep(0.2)
            stop.set()
            return True

    monkeypatch.setattr(entry, 'write_capability_evidence', lambda *args, **kwargs: writes.append(kwargs['result']))
    monkeypatch.setattr(entry, '_evidence_path', lambda: tmp_path / 'capability.json')
    assert entry.run_loop(
        SlowWorker, poll_seconds=0, heartbeat_seconds=0.05, stop_event=stop,
    ) == 0
    assert started.is_set()
    assert writes.count('valid') >= 2


def test_heartbeat_publishes_failed_when_worker_health_is_bad(monkeypatch, tmp_path):
    from scripts import run_purge_batch_worker as entry

    writes = []
    stop = threading.Event()

    class UnhealthyWorker:
        def capability_healthy(self):
            return False
        def process_one(self):
            stop.set()
            return False

    monkeypatch.setattr(entry, 'write_capability_evidence', lambda *args, **kwargs: writes.append(kwargs['result']))
    monkeypatch.setattr(entry, '_evidence_path', lambda: tmp_path / 'capability.json')
    assert entry.run_loop(UnhealthyWorker, poll_seconds=0, heartbeat_seconds=0.01, stop_event=stop) == 0
    assert writes == ['failed']


def test_loop_preflights_identity_before_first_capability_publication(monkeypatch, tmp_path):
    from scripts import run_purge_batch_worker as entry
    writes = []
    stop = threading.Event()
    calls = []

    class Worker:
        def preflight(self): calls.append('preflight'); return False
        def capability_healthy(self): return False
        def process_one(self): stop.set(); return False

    monkeypatch.setattr(entry, 'write_capability_evidence', lambda *args, **kwargs: writes.append(kwargs['result']))
    monkeypatch.setattr(entry, '_evidence_path', lambda: tmp_path / 'capability.json')
    assert entry.run_loop(Worker, poll_seconds=0, heartbeat_seconds=0.01, stop_event=stop) == 0
    assert calls == ['preflight']
    assert writes == ['failed']


def test_shared_session_asset_lookup_leaves_clean_transaction_for_reference_snapshot():
    from app import create_app
    from models import ImageAsset, PurgeBatch, PurgeBatchItem, db
    from services.postgres_reference_snapshot import PostgresReferenceSnapshotReader
    from services.purge_batch_control import PurgeBatchControlService

    app = create_app('testing')
    with app.app_context():
        db.create_all()
        asset = ImageAsset(
            source_provider='test', source_bucket='bucket', source_relative_path='one.png', source_revision=1,
            display_name='one.png', oss_path='original/one', preview_oss_path='preview/one', content_hash='a' * 64,
            source_size=1, source_mime_type='image/png', source_width=1, source_height=1, vector=[0.0] * 1024,
            embedding_model='tongyi-embedding-vision-plus-2026-03-06', embedding_dimension=1024,
            normalization_version='preview-v1', status='archived',
        )
        batch = PurgeBatch(actor_id='admin', idempotency_key='key.shared.01', request_fingerprint_sha256='b' * 64,
                           confirmation_text='永久删除 1 张', status='object_backup')
        db.session.add_all([asset, batch])
        db.session.flush()
        db.session.add(PurgeBatchItem(batch_id=batch.id, target_asset_id=asset.id, ordinal=0))
        asset_id = str(asset.id)
        db.session.commit()
        service = PurgeBatchControlService(db.session)
        assert tuple(map(str, service.item_asset_ids(batch.id))) == (asset_id,)
        snapshot = PostgresReferenceSnapshotReader(db.session).capture_for_purge((asset_id,))
        assert snapshot.targets[0].asset_id == asset_id
        db.session.remove()
