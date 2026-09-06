import json
import uuid
from datetime import datetime, timedelta, timezone


def _context():
    from services.purge_formal_deletion_capability import FormalDeletionContext

    return FormalDeletionContext(
        environment_id="prod-cn-shanghai-primary",
        deployment_sha256="c" * 64,
        batch_id=uuid.uuid4(),
        asset_ids=(uuid.uuid4(),),
        database_manifest_sha256="d" * 64,
        object_manifest_sha256="e" * 64,
        formal_bucket="formal-images-private",
    )


def _payload(context, now):
    return {
        "schema_version": 1,
        "result": "valid",
        "grant_id": "change-28-pilot-001",
        "environment_id": context.environment_id,
        "deployment_sha256": context.deployment_sha256,
        "batch_id": str(context.batch_id),
        "asset_ids": [str(value) for value in context.asset_ids],
        "max_batches": 1,
        "max_assets": 1,
        "max_object_deletes": 2,
        "database_manifest_sha256": context.database_manifest_sha256,
        "object_manifest_sha256": context.object_manifest_sha256,
        "formal_bucket": context.formal_bucket,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "trust": {
            "schema_version": 1,
            "mode": "fenced_writers_iam_no_overwrite",
            "result": "valid",
            "environment_id": context.environment_id,
            "formal_bucket": context.formal_bucket,
            "iam_policy_sha256": "a" * 64,
            "writer_inventory_sha256": "b" * 64,
            "verified_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=12)).isoformat(),
        },
    }


def test_file_capability_returns_typed_grant_only_for_exact_context(tmp_path):
    from services.purge_formal_deletion_capability import (
        FileFormalDeletionCapabilitySource,
        FormalDeletionGrant,
    )

    now = datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)
    context = _context()
    path = tmp_path / "formal-grant.json"
    path.write_text(json.dumps(_payload(context, now)), encoding="utf-8")
    source = FileFormalDeletionCapabilitySource(
        path, enabled=True, required_writer_inventory_sha256="b" * 64,
    )

    grant = source.evaluate(context, now=now + timedelta(minutes=1))

    assert isinstance(grant, FormalDeletionGrant)
    assert grant.grant_id == "change-28-pilot-001"
    assert grant.context == context
    assert grant.max_object_deletes == 2
    assert len(grant.trust_attestation_sha256) == 64


def test_file_capability_disabled_mismatch_or_expiry_is_unavailable(tmp_path):
    from services.purge_formal_deletion_capability import (
        FileFormalDeletionCapabilitySource,
    )

    now = datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc)
    context = _context()
    path = tmp_path / "formal-grant.json"
    path.write_text(json.dumps(_payload(context, now)), encoding="utf-8")
    assert FileFormalDeletionCapabilitySource(
        path, enabled=False, required_writer_inventory_sha256="b" * 64,
    ).evaluate(
        context, now=now,
    ) is None

    wrong = context.__class__(
        **{**context.__dict__, "deployment_sha256": "9" * 64}
    )
    source = FileFormalDeletionCapabilitySource(
        path, enabled=True, required_writer_inventory_sha256="b" * 64,
    )
    assert source.evaluate(wrong, now=now) is None
    assert source.evaluate(context, now=now + timedelta(minutes=11)) is None

    wrong_inventory = FileFormalDeletionCapabilitySource(
        path, enabled=True, required_writer_inventory_sha256="9" * 64,
    )
    assert wrong_inventory.evaluate(context, now=now) is None
