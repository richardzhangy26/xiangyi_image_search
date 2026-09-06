"""Short-lived binding ownership leases for formal objects."""

import uuid
from datetime import datetime

from sqlalchemy import Uuid

from . import db


class ObjectBindingFence(db.Model):
    __tablename__ = 'object_binding_fences'

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    formal_bucket = db.Column(db.String(255), nullable=False)
    formal_key = db.Column(db.Text, nullable=False)
    owner_kind = db.Column(db.String(32), nullable=False)
    owner_token = db.Column(Uuid(as_uuid=True), nullable=False)
    owner_generation = db.Column(db.BigInteger, nullable=False, default=0)
    state = db.Column(db.String(16), nullable=False, default='held')
    acquired_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    lease_expires_at = db.Column(db.DateTime, nullable=False)
    released_at = db.Column(db.DateTime, nullable=True)
    release_reason = db.Column(db.String(32), nullable=True)

    __table_args__ = (
        db.CheckConstraint(
            "owner_kind IN ('asset_ingest', 'import_promotion', 'import_cleanup')",
            name='ck_object_binding_fences_owner_kind',
        ),
        db.CheckConstraint(
            "state IN ('held', 'released')", name='ck_object_binding_fences_state',
        ),
        db.CheckConstraint(
            'lease_expires_at > acquired_at',
            name='ck_object_binding_fences_lease_order',
        ),
        db.CheckConstraint(
            "(state = 'held' AND released_at IS NULL AND release_reason IS NULL) OR "
            "(state = 'released' AND released_at IS NOT NULL AND release_reason IS NOT NULL)",
            name='ck_object_binding_fences_release_state',
        ),
        db.Index(
            'uq_object_binding_fences_held_identity', 'formal_bucket', 'formal_key',
            unique=True, postgresql_where=db.text("state = 'held'"),
        ),
        db.Index('idx_object_binding_fences_owner_expiry', 'owner_token', 'state', 'lease_expires_at'),
        db.Index('idx_object_binding_fences_identity_expiry', 'formal_bucket', 'formal_key', 'state', 'lease_expires_at'),
    )
