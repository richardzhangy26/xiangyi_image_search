"""Persisted T14 grant consumption and one-per-operation delete permits."""

import uuid
from datetime import datetime

from sqlalchemy import Uuid

from . import db


class FormalDeletionGrantConsumption(db.Model):
    __tablename__ = 'formal_deletion_grant_consumptions'

    grant_id = db.Column(db.String(128), primary_key=True)
    batch_id = db.Column(Uuid(as_uuid=True), nullable=False, unique=True)
    environment_id = db.Column(db.String(128), nullable=False)
    deployment_sha256 = db.Column(db.String(64), nullable=False)
    database_manifest_sha256 = db.Column(db.String(64), nullable=False)
    object_manifest_sha256 = db.Column(db.String(64), nullable=False)
    formal_bucket = db.Column(db.String(255), nullable=False)
    asset_scope_sha256 = db.Column(db.String(64), nullable=False)
    max_assets = db.Column(db.SmallInteger, nullable=False)
    max_object_deletes = db.Column(db.SmallInteger, nullable=False)
    used_object_deletes = db.Column(db.SmallInteger, nullable=False, default=0)
    issued_at = db.Column(db.DateTime, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    consumed_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    state = db.Column(db.String(16), nullable=False, default='active')
    trust_attestation_sha256 = db.Column(db.String(64), nullable=False)
    audit_retain_until = db.Column(db.DateTime, nullable=False)

    __table_args__ = (
        db.CheckConstraint(
            "state IN ('active', 'closed', 'expired')",
            name='ck_formal_deletion_grant_state',
        ),
        db.CheckConstraint(
            'max_assets BETWEEN 1 AND 20',
            name='ck_formal_deletion_grant_max_assets',
        ),
        db.CheckConstraint(
            'max_object_deletes BETWEEN 1 AND 40',
            name='ck_formal_deletion_grant_max_deletes',
        ),
        db.CheckConstraint(
            'used_object_deletes >= 0 AND used_object_deletes <= max_object_deletes',
            name='ck_formal_deletion_grant_used_deletes',
        ),
        db.CheckConstraint(
            'expires_at > issued_at',
            name='ck_formal_deletion_grant_time_order',
        ),
        db.Index('idx_formal_deletion_grant_batch', 'batch_id', unique=True),
        db.Index('idx_formal_deletion_grant_expiry', 'state', 'expires_at'),
    )


class FormalDeleteCallPermit(db.Model):
    __tablename__ = 'formal_delete_call_permits'

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    grant_id = db.Column(
        db.String(128),
        db.ForeignKey('formal_deletion_grant_consumptions.grant_id'),
        nullable=False,
    )
    batch_id = db.Column(Uuid(as_uuid=True), nullable=False)
    target_asset_id = db.Column(Uuid(as_uuid=True), nullable=False)
    operation_kind = db.Column(db.String(16), nullable=False)
    claim_generation = db.Column(db.BigInteger, nullable=False)
    formal_bucket = db.Column(db.String(255), nullable=False)
    formal_key = db.Column(db.Text, nullable=False)
    object_size = db.Column(db.BigInteger, nullable=False)
    object_sha256 = db.Column(db.String(64), nullable=False)
    object_etag = db.Column(db.Text, nullable=False)
    original_fence_id = db.Column(Uuid(as_uuid=True), nullable=False)
    preview_fence_id = db.Column(Uuid(as_uuid=True), nullable=False)
    state = db.Column(db.String(16), nullable=False, default='issued')
    issued_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    executing_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    result_code = db.Column(db.String(80), nullable=True)
    audit_retain_until = db.Column(db.DateTime, nullable=False)

    __table_args__ = (
        db.UniqueConstraint(
            'batch_id', 'target_asset_id', 'operation_kind',
            name='uq_formal_delete_permit_item_operation',
        ),
        db.CheckConstraint(
            "operation_kind IN ('original', 'preview')",
            name='ck_formal_delete_permit_operation',
        ),
        db.CheckConstraint(
            "state IN ('issued', 'executing', 'completed', 'cancelled')",
            name='ck_formal_delete_permit_state',
        ),
        db.CheckConstraint(
            'object_size > 0', name='ck_formal_delete_permit_object_size',
        ),
        db.CheckConstraint(
            'expires_at > issued_at', name='ck_formal_delete_permit_time_order',
        ),
        db.CheckConstraint(
            "(state = 'issued' AND executing_at IS NULL AND completed_at IS NULL AND cancelled_at IS NULL) OR "
            "(state = 'executing' AND executing_at IS NOT NULL AND completed_at IS NULL AND cancelled_at IS NULL) OR "
            "(state = 'completed' AND executing_at IS NOT NULL AND completed_at IS NOT NULL AND cancelled_at IS NULL) OR "
            "(state = 'cancelled' AND executing_at IS NULL AND completed_at IS NULL AND cancelled_at IS NOT NULL)",
            name='ck_formal_delete_permit_state_times',
        ),
        db.Index('idx_formal_delete_permit_grant_state', 'grant_id', 'state'),
        db.Index(
            'idx_formal_delete_permit_batch_item',
            'batch_id', 'target_asset_id', 'operation_kind',
        ),
    )
