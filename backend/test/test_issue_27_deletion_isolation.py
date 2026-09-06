from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_t13_formal_deletion_capability_is_unavailable_by_default():
    from services.purge_formal_deletion_capability import (
        UnavailableFormalDeletionCapabilitySource,
    )

    assert UnavailableFormalDeletionCapabilitySource().evaluate() is False


def test_purge_worker_does_not_name_delete_credentials_or_adapter():
    source = (BACKEND_DIR / 'scripts/run_purge_batch_worker.py').read_text(
        encoding='utf-8'
    )
    assert 'PURGE_DELETE_OSS_' not in source
    assert 'OssFormalObjectDeleter' not in source
    assert 'delete_if_present' not in source
