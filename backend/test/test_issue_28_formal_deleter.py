import io
import uuid
from datetime import datetime, timedelta, timezone


class MissingObject(Exception):
    status = 404


class HeadResult:
    def __init__(self, data):
        self.content_length = len(data)
        self.headers = {"ETag": f'"etag-{len(data)}"'}


class FakeBucket:
    def __init__(self, key, data):
        self.objects = {key: data}
        self.delete_calls = []

    def head_object(self, key):
        if key not in self.objects:
            raise MissingObject()
        return HeadResult(self.objects[key])

    def get_object(self, key):
        if key not in self.objects:
            raise MissingObject()
        return io.BytesIO(self.objects[key])

    def delete_object(self, key):
        self.delete_calls.append(key)
        self.objects.pop(key, None)


def _authorization(observation, now):
    from services.formal_purge import DeleteCallAuthorization

    return DeleteCallAuthorization(
        permit_id=uuid.uuid4(),
        grant_id="test-grant",
        batch_id=uuid.uuid4(),
        target_asset_id=uuid.uuid4(),
        claim_token=uuid.uuid4(),
        claim_generation=1,
        operation_kind="original",
        formal_bucket=observation.formal_bucket,
        formal_key=observation.formal_key,
        fence_ids=(uuid.uuid4(), uuid.uuid4()),
        observation=observation,
        authorized_at=now,
        expires_at=now + timedelta(seconds=60),
    )


def test_deleter_reobserves_exact_bytes_then_returns_typed_deletion_observation():
    from services.purge_object_storage import (
        DeletionObservation,
        OssFormalObjectDeleter,
    )

    now = datetime(2026, 8, 31, 6, 0, tzinfo=timezone.utc)
    key = "original/asset-a"
    bucket = FakeBucket(key, b"formal-object-bytes")
    deleter = OssFormalObjectDeleter(
        bucket=bucket,
        formal_bucket="formal-images-private",
        permit_verifier=lambda authorization: authorization,
        clock=lambda: now,
    )
    observation = deleter.observe(key)

    result = deleter.delete(_authorization(observation, now))

    assert isinstance(result, DeletionObservation)
    assert result.result == "deleted"
    assert result.before == observation
    assert result.after_missing is True
    assert bucket.delete_calls == [key]


def test_deleter_rejects_identity_change_before_delete():
    from services.purge_object_storage import OssFormalObjectDeleter

    now = datetime(2026, 8, 31, 6, 0, tzinfo=timezone.utc)
    key = "original/asset-a"
    bucket = FakeBucket(key, b"first")
    deleter = OssFormalObjectDeleter(
        bucket=bucket,
        formal_bucket="formal-images-private",
        permit_verifier=lambda authorization: authorization,
        clock=lambda: now,
    )
    observation = deleter.observe(key)
    bucket.objects[key] = b"changed-after-authorization"

    try:
        deleter.delete(_authorization(observation, now))
    except RuntimeError as exc:
        assert getattr(exc, "error_code") == "PURGE_OBJECT_IDENTITY_MISMATCH"
    else:
        raise AssertionError("changed bytes must never be deleted")
    assert bucket.delete_calls == []


def test_deleter_rejects_handmade_dto_when_persisted_permit_verifier_denies():
    from services.purge_object_storage import OssFormalObjectDeleter

    now = datetime(2026, 8, 31, 6, 0, tzinfo=timezone.utc)
    key = "original/asset-a"
    bucket = FakeBucket(key, b"formal-object-bytes")
    observer = OssFormalObjectDeleter(
        bucket=bucket, formal_bucket="formal-images-private",
        permit_verifier=lambda authorization: authorization,
        clock=lambda: now,
    )
    observation = observer.observe(key)
    handmade = _authorization(observation, now)
    rejecting = OssFormalObjectDeleter(
        bucket=bucket, formal_bucket="formal-images-private",
        permit_verifier=lambda _authorization: None,
        clock=lambda: now,
    )

    try:
        rejecting.delete(handmade)
    except RuntimeError as exc:
        assert getattr(exc, "error_code") == "PURGE_FORMAL_DELETION_DISABLED"
    else:
        raise AssertionError("unpersisted permit must never reach Delete")
    assert bucket.delete_calls == []
