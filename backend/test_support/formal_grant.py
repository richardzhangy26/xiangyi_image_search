import uuid
from datetime import datetime, timedelta, timezone

from services.purge_formal_deletion_capability import (
    FormalDeletionContext,
    FormalDeletionGrant,
)


def formal_grant_for(
    batch,
    item,
    *,
    grant_id=None,
    max_object_deletes=2,
    now=None,
):
    moment = now or datetime.now(timezone.utc)
    context = FormalDeletionContext(
        environment_id='test',
        deployment_sha256='1' * 64,
        batch_id=batch.id,
        asset_ids=(item.target_asset_id,),
        database_manifest_sha256=batch.database_manifest_sha256,
        object_manifest_sha256=batch.object_manifest_sha256,
        formal_bucket=item.formal_bucket,
    )
    return FormalDeletionGrant(
        grant_id=grant_id or f'grant-{uuid.uuid4()}',
        context=context,
        max_object_deletes=max_object_deletes,
        issued_at=moment,
        expires_at=moment + timedelta(minutes=10),
        trust_attestation_sha256='2' * 64,
    )


class StaticFormalCapability:
    def __init__(self, grant):
        self.grant = grant

    def evaluate(self, context=None):
        return self.grant if context in (None, self.grant.context) else None
