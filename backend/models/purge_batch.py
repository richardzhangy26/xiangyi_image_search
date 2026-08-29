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
    'failed',
    'cancelled',
)
CLAIMABLE_BATCH_STATUSES = ('queued', 'database_backup', 'object_backup', 'verifying')


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
    failed_at = db.Column(db.DateTime, nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint(
            'actor_id', 'idempotency_key', name='uq_purge_batches_actor_key'
        ),
        db.CheckConstraint(
            "status IN ('queued', 'database_backup', 'object_backup', 'verifying', "
            "'pending_deletion', 'failed', 'cancelled')",
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
            'items': [item.to_public_dict() for item in self.items],
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
    status = db.Column(db.String(24), nullable=False, default='queued')
    result_code = db.Column(db.String(80), nullable=True)
    error_code = db.Column(db.String(80), nullable=True)
    checkpoint_at = db.Column(db.DateTime, nullable=True)

    batch = db.relationship('PurgeBatch', back_populates='items')

    __table_args__ = (
        db.CheckConstraint('ordinal >= 0', name='ck_purge_batch_items_ordinal'),
    )

    def to_public_dict(self):
        return {
            'asset_id': str(self.target_asset_id),
            'status': self.status,
            'result_code': self.result_code,
            'error_code': self.error_code,
            'checkpoint_at': _isoformat(self.checkpoint_at),
        }
