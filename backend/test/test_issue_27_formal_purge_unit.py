from services.purge_formal_deletion_capability import (
    UnavailableFormalDeletionCapabilitySource,
)
from datetime import datetime, timezone


def test_unavailable_capability_never_claims_or_calls_formal_deleter():
    from services.formal_purge import FormalPurgeWorker

    class Repository:
        def __init__(self):
            self.claims = 0

        def claim_next_item(self):
            self.claims += 1
            return object()

    class Deleter:
        def delete_if_present(self, *_args, **_kwargs):
            raise AssertionError('硬关闭时不得调用删除协议')

    repository = Repository()
    worker = FormalPurgeWorker(
        repository=repository,
        capability=UnavailableFormalDeletionCapabilitySource(),
        deleter=Deleter(),
    )

    assert worker.process_one_item() is False
    assert repository.claims == 0


def test_fake_deleter_checkpoint_order_keeps_shared_preview_and_finalizes_item():
    from services.formal_purge import FormalPurgeWorker
    from services.purge_object_storage import (
        DeletionObservation,
        FormalObjectObservation,
    )

    calls = []
    now = datetime(2026, 8, 31, 6, 0, tzinfo=timezone.utc)
    observation = FormalObjectObservation(
        formal_bucket='formal', formal_key='original/a', size=1,
        sha256='a' * 64, etag='etag', observed_at=now,
    )
    authorization = object()
    deletion = DeletionObservation(
        result='deleted', before=observation, deleted_at=now,
        after_missing=True,
    )

    class Capability:
        def evaluate(self):
            return True

    class Repository:
        def reconcile_expired_authorizations(self, **_kwargs):
            return 0

        def claim_next_item(self):
            return {'original': 'original/a', 'preview': 'preview/shared'}

        def checkpoint(self, _claim, checkpoint, **_kwargs):
            calls.append(checkpoint)
            return True

        def preview_is_shared(self, _claim):
            return True
        def begin_delete_intent(self, *_args, **kwargs):
            assert kwargs['observation'] is observation
            calls.append('original_delete_started')
            calls.append('authorized:original')
            return authorization

        def start_delete_call(self, received):
            return received

        def complete_delete_call(self, received, result):
            assert received is authorization and result is deletion
            calls.append('original_deleted')
            return True

        def finalize(self, _claim):
            calls.append('database_deleted')
            return True

    class Deleter:
        def observe(self, key):
            calls.append(f'observe:{key}')
            return observation

        def delete(self, received):
            assert received is authorization
            calls.append('delete:authorized')
            return deletion

    assert FormalPurgeWorker(
        repository=Repository(), capability=Capability(), deleter=Deleter(),
    ).process_one_item() is True
    assert calls == [
        'observe:original/a', 'original_delete_started', 'authorized:original',
        'delete:authorized', 'original_deleted',
        'preview_shared', 'database_deleted', 'completed',
    ]


def test_repository_requires_explicit_canonical_authorization_verifier():
    from services.formal_purge import FormalPurgeRepository

    try:
        FormalPurgeRepository(object())
    except ValueError as exc:
        assert 'verifier' in str(exc)
    else:
        raise AssertionError('repository must fail closed without verifier')


def test_worker_never_calls_deleter_when_authorization_rejects():
    from services.formal_purge import FormalPurgeWorker

    calls = []
    class Capability:
        def evaluate(self): return True
    class Repository:
        def reconcile_expired_authorizations(self, **_kwargs): return 0
        def claim_next_item(self): return {'checkpoint': 'pending', 'original': 'o', 'preview': 'p'}
        def checkpoint(self, *_args, **_kwargs): return True
        def begin_delete_intent(self, *_args, **_kwargs): return None
        def fail(self, *_args, **_kwargs): return True
    class Deleter:
        def delete_if_present(self, key): calls.append(key)

    assert FormalPurgeWorker(repository=Repository(), capability=Capability(), deleter=Deleter()).process_one_item() is True
    assert calls == []


def test_capability_revoked_after_claim_prevents_first_irreversible_call():
    from services.formal_purge import FormalPurgeWorker

    delete_calls = []

    class Capability:
        def __init__(self):
            self.results = [True, False]

        def evaluate(self, _context=None):
            return self.results.pop(0)

    class Repository:
        def reconcile_expired_authorizations(self, **_kwargs):
            return 0

        def claim_next_item(self):
            return {
                'checkpoint': 'pending',
                'original': 'original/a',
                'preview': 'preview/a',
            }

        def checkpoint(self, *_args, **_kwargs):
            return True

        def fail(self, *_args, **_kwargs):
            return True

    class Deleter:
        def delete_if_present(self, key):
            delete_calls.append(key)

    worker = FormalPurgeWorker(
        repository=Repository(),
        capability=Capability(),
        capability_context=object(),
        deleter=Deleter(),
    )

    assert worker.process_one_item() is True
    assert delete_calls == []


def test_worker_reconciles_expired_authorizations_before_claim():
    from services.formal_purge import FormalPurgeWorker

    calls = []

    class Capability:
        def evaluate(self):
            return True

    class Repository:
        def reconcile_expired_authorizations(self, **_kwargs):
            calls.append('reconcile')
            return 0

        def claim_next_item(self):
            calls.append('claim')
            return None

    class Deleter:
        def delete(self, *_args, **_kwargs):
            raise AssertionError('reconcile 路径不得 Delete')

    worker = FormalPurgeWorker(
        repository=Repository(),
        capability=Capability(),
        deleter=Deleter(),
    )

    assert worker.process_one_item() is False
    assert calls == ['reconcile', 'claim']
