"""持久图片导入任务模型。"""

import uuid
from datetime import datetime

from sqlalchemy import Uuid

from . import db


EXPECTED_EMBEDDING_MODEL = 'tongyi-embedding-vision-plus-2026-03-06'
EXPECTED_EMBEDDING_DIMENSION = 1024

# 可被取消的状态集合（Issue #21；汇合 #20 后并入 awaiting_retry）。
CANCELABLE_STATUSES = ('queued', 'embedding', 'failed', 'awaiting_retry')


class ImageImportItem(db.Model):
    """已写入私有对象存储、等待独立 worker 生成向量的图片。"""

    __tablename__ = 'image_import_items'

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    source_provider = db.Column(db.String(32), nullable=False)
    source_bucket = db.Column(db.String(255), nullable=False)
    source_relative_path = db.Column(db.Text, nullable=False)
    source_revision = db.Column(db.Integer, nullable=False, default=1)
    display_name = db.Column(db.Text, nullable=False)

    oss_path = db.Column(db.Text, nullable=False)
    preview_oss_path = db.Column(db.Text, nullable=False)
    content_hash = db.Column(db.String(64), nullable=False)
    source_size = db.Column(db.BigInteger, nullable=False)
    source_mime_type = db.Column(db.String(100), nullable=False)
    source_width = db.Column(db.Integer, nullable=False)
    source_height = db.Column(db.Integer, nullable=False)
    normalization_version = db.Column(db.String(32), nullable=False)

    expected_embedding_model = db.Column(
        db.String(128),
        nullable=False,
        default='tongyi-embedding-vision-plus-2026-03-06',
    )
    expected_embedding_dimension = db.Column(
        db.SmallInteger,
        nullable=False,
        default=1024,
    )
    status = db.Column(db.String(20), nullable=False, default='queued')
    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    last_error_class = db.Column(db.String(32), nullable=True)
    last_attempt_at = db.Column(db.DateTime, nullable=True)
    next_retry_at = db.Column(db.DateTime, nullable=True)
    asset_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey('image_assets.id', ondelete='SET NULL'),
        nullable=True,
    )
    request_id = db.Column(db.String(64), nullable=False)

    claim_token = db.Column(Uuid(as_uuid=True), nullable=True)
    claim_generation = db.Column(db.BigInteger, nullable=False, default=0)
    claimed_by = db.Column(db.String(128), nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    lease_expires_at = db.Column(db.DateTime, nullable=True)
    embedding_started_at = db.Column(db.DateTime, nullable=True)

    completed_at = db.Column(db.DateTime, nullable=True)
    failed_at = db.Column(db.DateTime, nullable=True)
    failure_message = db.Column(db.String(512), nullable=True)
    cancel_requested_at = db.Column(db.DateTime, nullable=True)
    cancel_requested_by = db.Column(db.String(128), nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)
    purge_eligible_at = db.Column(db.DateTime, nullable=True)
    objects_purged_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.now,
        onupdate=datetime.now,
    )

    asset = db.relationship('ImageAsset', foreign_keys=[asset_id])

    __table_args__ = (
        db.UniqueConstraint(
            'source_provider',
            'source_bucket',
            'source_relative_path',
            'source_revision',
            name='uq_image_import_items_source_identity',
        ),
        db.CheckConstraint(
            "status IN ('queued', 'embedding', 'completed', 'failed',"
            " 'awaiting_retry', 'cancelled', 'abandoned')",
            name='ck_image_import_items_status_v2',
        ),
        db.CheckConstraint(
            'attempt_count >= 0',
            name='ck_image_import_items_attempt_count',
        ),
        db.CheckConstraint(
            'source_revision >= 1',
            name='ck_image_import_items_source_revision',
        ),
        db.CheckConstraint(
            "expected_embedding_model = 'tongyi-embedding-vision-plus-2026-03-06'",
            name='ck_image_import_items_embedding_model',
        ),
        db.CheckConstraint(
            'expected_embedding_dimension = 1024',
            name='ck_image_import_items_embedding_dimension',
        ),
        db.CheckConstraint(
            'claim_generation >= 0',
            name='ck_image_import_items_claim_generation',
        ),
        db.Index('idx_image_import_items_claim_order', 'status', 'created_at', 'id'),
        db.Index('idx_image_import_items_lease', 'status', 'lease_expires_at'),
        db.Index(
            'idx_image_import_items_retry_schedule', 'status', 'next_retry_at'
        ),
    )

    def to_public_dict(self):
        """返回可持久恢复的安全状态，不暴露私有对象键或向量。"""
        # 局部导入避免在模型层建立对服务层的顶层依赖。
        from services.import_retry import MAX_AUTO_ATTEMPTS

        recovery_action = (
            {
                'type': 'open_recycle_bin',
                'asset_id': str(self.asset_id),
            }
            if self.asset_id and self.asset and self.asset.status == 'archived'
            else None
        )
        return {
            'item_id': str(self.id),
            'display_name': self.display_name,
            'source_relative_path': self.source_relative_path,
            'source_revision': self.source_revision,
            'status': self.status,
            'asset_id': str(self.asset_id) if self.asset_id else None,
            'failure_message': self.failure_message,
            'attempt_count': self.attempt_count,
            'max_auto_attempts': MAX_AUTO_ATTEMPTS,
            'last_error_class': self.last_error_class,
            'last_attempt_at': (
                self.last_attempt_at.isoformat()
                if self.last_attempt_at
                else None
            ),
            'next_retry_at': (
                self.next_retry_at.isoformat() if self.next_retry_at else None
            ),
            'cancel_requested_at': (
                self.cancel_requested_at.isoformat()
                if self.cancel_requested_at
                else None
            ),
            'cancelled_at': (
                self.cancelled_at.isoformat() if self.cancelled_at else None
            ),
            'purge_eligible_at': (
                self.purge_eligible_at.isoformat()
                if self.purge_eligible_at
                else None
            ),
            'objects_purged_at': (
                self.objects_purged_at.isoformat()
                if self.objects_purged_at
                else None
            ),
            'recovery_action': recovery_action,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'embedding_started_at': (
                self.embedding_started_at.isoformat()
                if self.embedding_started_at
                else None
            ),
            'completed_at': (
                self.completed_at.isoformat() if self.completed_at else None
            ),
            'failed_at': self.failed_at.isoformat() if self.failed_at else None,
        }
