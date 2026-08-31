"""Formal OSS bucket identity used by Issue #27 fences.

The bucket is a storage-role configuration value, never an asset's source
provenance field.
"""


class FormalBucketIdentityProvider:
    def __init__(self, bucket_name: str):
        if not isinstance(bucket_name, str) or not bucket_name.strip():
            raise ValueError('正式对象 Bucket 不能为空')
        self._bucket_name = bucket_name.strip()

    def formal_bucket(self) -> str:
        return self._bucket_name
