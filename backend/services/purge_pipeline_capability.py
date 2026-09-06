"""Worker 能力证明的无凭证、失败关闭读取器。"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


CAPABILITY_FILENAME = 'purge_batch_worker.json'
CAPABILITY_TTL_SECONDS = 120
CAPABILITY_HEARTBEAT_SECONDS = 30
MAX_CAPABILITY_BYTES = 65536
_ALLOWED = frozenset({'schema_version', 'component', 'result', 'verified_at', 'expires_at', 'policy', 'summary'})
_FORBIDDEN_PARTS = ('password', 'secret', 'token', 'authorization', 'dsn')


def _parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _has_forbidden_key(value):
    if isinstance(value, dict):
        return any(
            any(part in str(key).lower() for part in _FORBIDDEN_PARTS)
            or _has_forbidden_key(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_has_forbidden_key(item) for item in value)
    return False


class UnavailablePurgePipelineCapabilitySource:
    def evaluate(self, now):
        return False


class FilePurgePipelineCapabilitySource:
    def __init__(self, path: Path):
        self.path = Path(path)

    def evaluate(self, now):
        try:
            if not self.path.is_file() or self.path.stat().st_size > MAX_CAPABILITY_BYTES:
                return False
            payload = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict) or set(payload) != _ALLOWED or _has_forbidden_key(payload):
            return False
        verified_at, expires_at = _parse_time(payload['verified_at']), _parse_time(payload['expires_at'])
        return bool(
            payload['schema_version'] == 1
            and payload['component'] == 'purge_batch_worker'
            and payload['result'] == 'valid'
            and payload['policy'] == 'backup_only_no_delete'
            and isinstance(payload['summary'], str)
            and verified_at is not None
            and expires_at is not None
            and verified_at <= now.astimezone(timezone.utc) + timedelta(seconds=60)
            and expires_at > now.astimezone(timezone.utc)
            and expires_at <= verified_at + timedelta(seconds=CAPABILITY_TTL_SECONDS)
        )


def write_capability_evidence(path: Path, *, now: datetime, result: str, ttl_seconds=CAPABILITY_TTL_SECONDS, summary='worker ready'):
    """仅供 worker 使用的原子写入；不接受或记录任何敏感内容。"""
    if result not in {'valid', 'failed'}:
        raise ValueError('invalid capability result')
    moment = now.astimezone(timezone.utc)
    payload = {
        'schema_version': 1, 'component': 'purge_batch_worker', 'result': result,
        'verified_at': moment.isoformat(), 'expires_at': (moment + timedelta(seconds=ttl_seconds)).isoformat(),
        'policy': 'backup_only_no_delete', 'summary': str(summary)[:200],
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f'.{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    temporary.write_text(json.dumps(payload, separators=(',', ':')), encoding='utf-8')
    os.replace(temporary, target)
