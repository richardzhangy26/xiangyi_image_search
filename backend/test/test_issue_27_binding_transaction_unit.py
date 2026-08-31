def test_caller_owned_finalize_does_not_commit_or_begin():
    from services.object_binding_fence import ObjectBindingFenceService

    class Session:
        def begin(self): raise AssertionError('caller-owned finalize must not begin')
        def commit(self): raise AssertionError('caller-owned finalize must not commit')
        def rollback(self): raise AssertionError('caller-owned finalize must not rollback')

    service = ObjectBindingFenceService(Session())
    assert service.finalize_in_transaction(None, Session(), lambda: False) is False


def test_acquire_prewrite_requires_separate_control_session_factory():
    from services.object_binding_fence import ObjectBindingFenceService

    service = ObjectBindingFenceService(object())
    try:
        service.acquire_prewrite((), owner_kind='asset_ingest', control_session_factory=None)
    except ValueError as exc:
        assert 'control session' in str(exc)
    else:
        raise AssertionError('control session factory is mandatory')


def test_renew_prewrite_requires_separate_control_session_factory():
    from services.object_binding_fence import ObjectBindingFenceService

    try:
        ObjectBindingFenceService(object()).renew_prewrite(None, control_session_factory=None)
    except ValueError as exc:
        assert 'control session' in str(exc)
    else:
        raise AssertionError('control session factory is mandatory')


def test_caller_owned_finalize_rejects_missing_lease_before_callback():
    from services.object_binding_fence import ObjectBindingFenceService

    called = []
    assert ObjectBindingFenceService(object()).finalize_in_transaction(
        None, object(), lambda: called.append('bind') or True,
    ) is False
    assert called == []
