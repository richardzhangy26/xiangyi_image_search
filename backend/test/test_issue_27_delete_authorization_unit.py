def test_delete_authorization_refuses_without_complete_purge_fence():
    from services.formal_purge import FormalPurgeRepository

    class Session:
        pass

    repository = FormalPurgeRepository(
        Session(), manifest_validator=lambda _b, _i: True,
    )
    assert repository.authorize_delete_call(None, 'original_delete_started', None, 'original') is None


def test_authorize_delete_call_requires_complete_original_preview_fence_set():
    from services.formal_purge import FormalPurgeRepository

    repository = FormalPurgeRepository(
        object(), manifest_validator=lambda _b, _i: True,
    )
    claim = type('Claim', (), {'batch_id': 'b', 'target_asset_id': 'a'})()
    authorization = type('Authorization', (), {'fence_ids': ('original-only',)})()
    assert repository.authorize_delete_call(
        claim, 'original_delete_started', authorization, 'original',
    ) is None
