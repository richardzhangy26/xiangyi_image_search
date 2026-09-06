import pytest


def test_formal_bucket_identity_rejects_empty_and_never_reads_source_metadata():
    from services.formal_bucket_identity import FormalBucketIdentityProvider

    provider = FormalBucketIdentityProvider('private-formal-bucket')
    assert provider.formal_bucket() == 'private-formal-bucket'

    with pytest.raises(ValueError):
        FormalBucketIdentityProvider('')
