"""Issue #26 永久清除批次的持久状态模型。

此模型只保存进入 ``pending_deletion`` 前的备份/校验证据摘要；不提供删除行为。
``target_asset_id`` 故意不是 image_assets 外键，以便后续删除资产行时仍保留批次审计墓碑。
"""

import uuid
from datetime import datetime

from sqlalchemy import Uuid

from . import db


PURGE_BATCH_STATUSES = (
    'queued',
    'database_backup',
    'object_backup',
    'verifying',
    'pending_deletion',
    'deleting',
    'partial_failure',
    'completed',
    'failed',
    'cancelled',
)
CLAIMABLE_BATCH_STATUSES = ('queued', 'database_backup', 'object_backup', 'verifying')

# #27 以单调检查点描述对象处置；失败是 item status，不是检查点。
FORMAL_PURGE_ITEM_CHECKPOINTS = (
    'pending',
    'fenced',
    'original_delete_started',
    'original_deleted',
    'preview_delete_started',
    'preview_deleted',
    'preview_shared',
    'completed',
)


def _isoformat(value):
    return value.isoformat() if value else None


class PurgeBatch(db.Model):
    """一个已确认的、可取消的备份与验证批次。"""

    __tablename__ = 'purge_batches'

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id = db.Column(db.String(128), nullable=False)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_fingerprint_sha256 = db.Column(db.String(64), nullable=False)
    confirmation_text = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(24), nullable=False, default='queued')

    claim_token = db.Column(Uuid(as_uuid=True), nullable=True)
    claim_generation = db.Column(db.BigInteger, nullable=False, default=0)
    claimed_by = db.Column(db.String(128), nullable=True)
    lease_expires_at = db.Column(db.DateTime, nullable=True)

    database_backup_id = db.Column(db.String(160), nullable=True)
    database_manifest_sha256 = db.Column(db.String(64), nullable=True)
    object_manifest_sha256 = db.Column(db.String(64), nullable=True)
    retain_until = db.Column(db.DateTime, nullable=True)
    error_code = db.Column(db.String(80), nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    deleting_at = db.Column(db.DateTime, nullable=True)
    partial_failure_at = db.Column(db.DateTime, nullable=True)
    failed_at = db.Column(db.DateTime, nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint(
            'actor_id', 'idempotency_key', name='uq_purge_batches_actor_key'
        ),
        db.CheckConstraint(
            "status IN ('queued', 'database_backup', 'object_backup', 'verifying', "
            "'pending_deletion', 'deleting', 'partial_failure', 'completed', "
            "'failed', 'cancelled')",
            name='ck_purge_batches_status',
        ),
        db.CheckConstraint(
            'claim_generation >= 0', name='ck_purge_batches_claim_generation'
        ),
        db.Index('idx_purge_batches_claim_order', 'status', 'created_at', 'id'),
        db.Index('idx_purge_batches_lease', 'status', 'lease_expires_at'),
    )

    items = db.relationship('PurgeBatchItem', back_populates='batch', lazy=True)

    def to_public_dict(self):
        """返回不含凭证、对象键或清单内容的批次 DTO。"""
        items = [item.to_public_dict() for item in self.items]
        return {
            'batch_id': str(self.id),
            'status': self.status,
            'error_code': self.error_code,
            'retain_until': _isoformat(self.retain_until),
            'created_at': _isoformat(self.created_at),
            'started_at': _isoformat(self.started_at),
            'completed_at': _isoformat(self.completed_at),
            'failed_at': _isoformat(self.failed_at),
            'cancelled_at': _isoformat(self.cancelled_at),
            'completed_count': sum(item['status'] == 'completed' for item in items),
            'failed_count': sum(item['status'] == 'failed' for item in items),
            'pending_count': sum(item['status'] not in {'completed', 'failed'} for item in items),
            'cancellable': self.status == 'pending_deletion' and self.deleting_at is None,
            'items': items,
        }


class PurgeBatchItem(db.Model):
    """批次内单张资产的安全检查点与结果摘要。"""

    __tablename__ = 'purge_batch_items'

    batch_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey('purge_batches.id', ondelete='CASCADE'),
        primary_key=True,
    )
    target_asset_id = db.Column(Uuid(as_uuid=True), primary_key=True)
    ordinal = db.Column(db.SmallInteger, nullable=False)
    status = db.Column(db.String(24), nullable=False, default='pending')
    result_code = db.Column(db.String(80), nullable=True)
    error_code = db.Column(db.String(80), nullable=True)
    checkpoint = db.Column(db.String(40), nullable=False, default='pending')
    checkpoint_at = db.Column(db.DateTime, nullable=True)
    claim_token = db.Column(Uuid(as_uuid=True), nullable=True)
    claim_generation = db.Column(db.BigInteger, nullable=False, default=0)
    lease_expires_at = db.Column(db.DateTime, nullable=True)
    original_delete_started_at = db.Column(db.DateTime, nullable=True)
    original_deleted_at = db.Column(db.DateTime, nullable=True)
    preview_delete_started_at = db.Column(db.DateTime, nullable=True)
    preview_deleted_at = db.Column(db.DateTime, nullable=True)
    preview_disposition = db.Column(db.String(40), nullable=True)
    database_deleted_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    failed_at = db.Column(db.DateTime, nullable=True)
    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    audit_retain_until = db.Column(db.DateTime, nullable=True)
    original_formal_key = db.Column(db.Text, nullable=True)
    original_backup_object_id = db.Column(db.String(128), nullable=True)
    original_backup_sha256 = db.Column(db.String(64), nullable=True)
    preview_formal_key = db.Column(db.Text, nullable=True)
    preview_backup_object_id = db.Column(db.String(128), nullable=True)
    preview_backup_sha256 = db.Column(db.String(64), nullable=True)
    preview_delete_authorized = db.Column(db.Boolean, nullable=False, default=False)
    authorization_retain_until = db.Column(db.DateTime, nullable=True)
    formal_bucket = db.Column(db.String(255), nullable=True)

    batch = db.relationship('PurgeBatch', back_populates='items')

    __table_args__ = (
        db.CheckConstraint('ordinal >= 0', name='ck_purge_batch_items_ordinal'),
        db.Index('idx_purge_batch_items_target', 'target_asset_id', 'batch_id'),
        db.Index(
            'idx_purge_batch_items_claim',
            'status', 'lease_expires_at', 'batch_id', 'ordinal',
        ),
    )

    def to_public_dict(self):
        next_action = (
            'retry_item' if self.status == 'failed' and self.result_code != 'nonretryable'
            else 'awaiting_protection' if self.status == 'failed'
            else 'none' if self.status == 'completed'
            else 'in_progress'
        )
        return {
            'asset_id': str(self.target_asset_id),
            'status': self.status,
            'result_code': self.result_code,
            'error_code': self.error_code,
            'checkpoint_at': _isoformat(self.checkpoint_at),
            'next_action': next_action,
        }
