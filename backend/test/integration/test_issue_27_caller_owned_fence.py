import uuid

from sqlalchemy import text

from models import ObjectBindingFence
from services.object_binding_fence import ObjectBindingFenceService
from services.purge_object_fence import ObjectIdentity


def _identities():
    return (
        ObjectIdentity('formal-test-bucket', 'original/caller.png'),
        ObjectIdentity('formal-test-bucket', 'preview/caller.jpg'),
    )


def test_caller_owned_finalize_rejects_generation_mismatch_without_callback(pg_session_factory):
    control, caller = pg_session_factory(), pg_session_factory()
    try:
        lease = ObjectBindingFenceService(control).acquire(_identities(), owner_kind='asset_ingest', lease_seconds=60)
        control.execute(text('UPDATE object_binding_fences SET owner_generation = owner_generation + 1 WHERE owner_token = :t'), {'t': str(lease.owner_token)})
        control.commit()
        calls = []
        with caller.begin():
            result = ObjectBindingFenceService(caller).finalize_in_transaction(lease, caller, lambda: calls.append('bind') or True)
            assert result is False
        assert calls == []
    finally:
        control.close(); caller.close()


def test_caller_owned_finalize_releases_only_when_outer_transaction_commits(pg_session_factory):
    control, caller = pg_session_factory(), pg_session_factory()
    try:
        lease = ObjectBindingFenceService(control).acquire(_identities(), owner_kind='asset_ingest', lease_seconds=60)
        calls = []
        with caller.begin():
            assert ObjectBindingFenceService(caller).finalize_in_transaction(lease, caller, lambda: calls.append('bind') or True)
        assert calls == ['bind']
        assert control.query(ObjectBindingFence).filter_by(owner_token=lease.owner_token, state='held').count() == 0
    finally:
        control.close(); caller.close()


def test_caller_owned_finalize_rejects_released_and_other_owner(pg_session_factory):
    control, caller = pg_session_factory(), pg_session_factory()
    try:
        lease = ObjectBindingFenceService(control).acquire(_identities(), owner_kind='asset_ingest', lease_seconds=60)
        control.execute(text("UPDATE object_binding_fences SET state='released', released_at=clock_timestamp(), release_reason='failed' WHERE owner_token=:t"), {'t': str(lease.owner_token)})
        control.commit()
        calls = []
        with caller.begin():
            assert ObjectBindingFenceService(caller).finalize_in_transaction(lease, caller, lambda: calls.append('bad') or True) is False
        assert calls == []
        replacement = ObjectBindingFenceService(control).acquire(_identities(), owner_kind='asset_ingest', lease_seconds=60)
        with caller.begin():
            assert ObjectBindingFenceService(caller).finalize_in_transaction(lease, caller, lambda: calls.append('bad2') or True) is False
        assert replacement.owner_token != lease.owner_token
        assert calls == []
    finally:
        control.close(); caller.close()


def test_caller_owned_finalize_outer_rollback_keeps_held_fence(pg_session_factory):
    control, caller = pg_session_factory(), pg_session_factory()
    try:
        lease = ObjectBindingFenceService(control).acquire(_identities(), owner_kind='asset_ingest', lease_seconds=60)
        transaction = caller.begin()
        assert ObjectBindingFenceService(caller).finalize_in_transaction(lease, caller, lambda: True)
        transaction.rollback()
        assert control.query(ObjectBindingFence).filter_by(owner_token=lease.owner_token, state='held').count() == 2
    finally:
        control.close(); caller.close()
