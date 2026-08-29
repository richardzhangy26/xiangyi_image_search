"""仅通过注入端口推进永久清除备份批次；不含删除能力。"""

from datetime import datetime, timezone
from pathlib import Path

from services.postgres_backup import BackupRequest
from services.purge_object_backup import PurgeObjectReferenceError


def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class PostgresRestorePointGate:
    """生产恢复点门：先创建/调和即时备份，再严格复验副本。"""

    def __init__(self, backup_service, *, now=None):
        self.backup_service = backup_service
        self.now = now or (lambda: datetime.now(timezone.utc))

    def require_verified(self, purge_batch_id: str):
        request = BackupRequest.restore_point(str(purge_batch_id))
        result = self.backup_service.create_backup(request)
        manifest_path = Path(self.backup_service.backup_root) / request.backup_id / 'manifest.json'
        if not manifest_path.is_file():
            raise FileNotFoundError('restore-point manifest missing')
        self.backup_service.verify_copies(manifest_path)
        if _as_utc(result.manifest.retain_until) <= _as_utc(self.now()):
            raise FileNotFoundError('restore-point retention expired')
        return result.manifest


class PurgeBatchWorker:
    def __init__(
        self,
        repository,
        *,
        worker_id,
        safety_ready,
        capability_available,
        phase_handlers=None,
        lease_seconds=60,
        identities_match=None,
        on_identity_failed=None,
    ):
        self.repository = repository
        self.worker_id = worker_id
        self.safety_ready = safety_ready
        self.capability_available = capability_available
        self.phase_handlers = dict(phase_handlers or {})
        self.lease_seconds = lease_seconds
        self.identities_match = identities_match or (lambda: True)
        self.on_identity_failed = on_identity_failed
        self._capability_healthy = True

    def capability_healthy(self):
        return self._capability_healthy

    def preflight(self):
        self._capability_healthy = bool(self.identities_match())
        if not self._capability_healthy and self.on_identity_failed is not None:
            self.on_identity_failed()
        return self._capability_healthy

    def process_one(self):
        if not self.preflight():
            return False
        if not self.safety_ready() or not self.capability_available():
            return False
        claim = self.repository.claim_next(worker_id=self.worker_id, lease_seconds=self.lease_seconds)
        if claim is None:
            return False
        try:
            handler = self.phase_handlers.get(claim.expected_status)
            if handler is None:
                return True
            next_status, evidence = self._unpack(handler(claim))
            if next_status:
                if not self.safety_ready() or not self.capability_available():
                    return True
                advanced = self.repository.advance_if_current(
                    claim, status=next_status, **evidence,
                )
                if advanced is False:
                    recorder = getattr(self.repository, 'record_stale_result', None)
                    if recorder is not None:
                        recorder(claim)
        except PurgeObjectReferenceError:
            self.repository.fail_if_current(
                claim, error_code='PURGE_REFERENCE_SNAPSHOT_INVALID', retryable=True,
            )
        except FileNotFoundError:
            self.repository.fail_if_current(
                claim, error_code='PURGE_BACKUP_RETENTION_EXPIRED', retryable=False,
            )
        except Exception:
            self.repository.fail_if_current(
                claim, error_code=self._phase_error(claim.expected_status), retryable=True,
            )
        return True

    @staticmethod
    def _unpack(outcome):
        if outcome is None:
            return None, {}
        if isinstance(outcome, tuple):
            status, evidence = outcome[0], outcome[1] if len(outcome) > 1 else {}
            return status, dict(evidence or {})
        return outcome, {}

    @staticmethod
    def _phase_error(status):
        return {
            'database_backup': 'PURGE_DATABASE_BACKUP_FAILED',
            'object_backup': 'PURGE_OBJECT_BACKUP_FAILED',
            'verifying': 'PURGE_OBJECT_VERIFICATION_FAILED',
        }.get(status, 'PURGE_REFERENCE_SNAPSHOT_INVALID')
