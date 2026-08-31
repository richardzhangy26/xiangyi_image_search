"""Issue #27 formal-object deletion fence epochs."""

import uuid
from datetime import datetime

from sqlalchemy import Uuid

from . import db


class PurgeObjectFence(db.Model):
    __tablename__ = 'purge_object_fences'

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    formal_bucket = db.Column(db.String(255), nullable=False)
    formal_key = db.Column(db.Text, nullable=False)
    kind = db.Column(db.String(24), nullable=False)
    batch_id = db.Column(Uuid(as_uuid=True), nullable=False)
    target_asset_id = db.Column(Uuid(as_uuid=True), nullable=False)
    state = db.Column(db.String(24), nullable=False, default='held')
    acquired_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    released_at = db.Column(db.DateTime, nullable=True)
    audit_retain_until = db.Column(db.DateTime, nullable=False)

    __table_args__ = (
        db.CheckConstraint(
            "state IN ('held', 'released')", name='ck_purge_object_fences_state'
        ),
        db.Index('idx_purge_object_fences_batch_item', 'batch_id', 'target_asset_id'),
        db.Index(
            'uq_purge_object_fences_held_identity',
            'formal_bucket',
            'formal_key',
            unique=True,
            postgresql_where=db.text("state = 'held'"),
        ),
    )
