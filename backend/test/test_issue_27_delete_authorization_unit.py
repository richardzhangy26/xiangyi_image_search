def test_delete_authorization_refuses_without_complete_purge_fence():
    from services.formal_purge import FormalPurgeRepository

    class Session:
        pass

    repository = FormalPurgeRepository(
        Session(), manifest_validator=lambda _b, _i: True,
    )
    assert repository.authorize_delete_call(
        None,
        expected_checkpoint='original_delete_started',
        operation_kind='original',
        observation=None,
    ) is None


def test_authorize_delete_call_rejects_untyped_object_observation_before_session_use():
    from services.formal_purge import FormalPurgeRepository

    repository = FormalPurgeRepository(
        object(), manifest_validator=lambda _b, _i: True,
    )
    claim = type('Claim', (), {'batch_id': 'b', 'target_asset_id': 'a'})()
    assert repository.authorize_delete_call(
        claim,
        expected_checkpoint='original_delete_started',
        operation_kind='original',
        observation=object(),
    ) is None
