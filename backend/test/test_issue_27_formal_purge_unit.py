from services.purge_formal_deletion_capability import (
    UnavailableFormalDeletionCapabilitySource,
)


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

    calls = []

    class Capability:
        def evaluate(self):
            return True

    class Repository:
        def claim_next_item(self):
            return {'original': 'original/a', 'preview': 'preview/shared'}

        def checkpoint(self, _claim, checkpoint, **_kwargs):
            calls.append(checkpoint)
            return True

        def preview_is_shared(self, _claim):
            return True
        def authorize_delete_call(self, *_args, **_kwargs): return object()

        def finalize(self, _claim):
            calls.append('database_deleted')
            return True

    class Deleter:
        def delete_if_present(self, key):
            calls.append(f'delete:{key}')
            return 'deleted'

    assert FormalPurgeWorker(
        repository=Repository(), capability=Capability(), deleter=Deleter(),
    ).process_one_item() is True
    assert calls == [
        'original_delete_started', 'delete:original/a', 'original_deleted',
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
        def claim_next_item(self): return {'checkpoint': 'pending', 'original': 'o', 'preview': 'p'}
        def checkpoint(self, *_args, **_kwargs): return True
        def authorize_delete_call(self, *_args, **_kwargs): return None
        def fail(self, *_args, **_kwargs): return True
    class Deleter:
        def delete_if_present(self, key): calls.append(key)

    assert FormalPurgeWorker(repository=Repository(), capability=Capability(), deleter=Deleter()).process_one_item() is True
    assert calls == []
