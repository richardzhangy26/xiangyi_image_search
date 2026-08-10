"""独立图片资产模型。"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Uuid, text

from . import db


class ImageAsset(db.Model):
    """可独立检索、可暂时不关联商品型号的图片资产。"""

    __tablename__ = 'image_assets'

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_number = db.Column(
        db.String(100),
        db.ForeignKey('products.model_number', ondelete='SET NULL'),
        nullable=True,
        comment='可选的商品型号',
    )

    source_provider = db.Column(db.String(32), nullable=False)
    source_bucket = db.Column(db.String(255), nullable=False)
    source_relative_path = db.Column(db.Text, nullable=False)
    source_revision = db.Column(db.Integer, nullable=False, default=1)

    oss_path = db.Column(db.Text, nullable=False, unique=True)
    preview_oss_path = db.Column(db.Text, nullable=False)
    content_hash = db.Column(db.String(64), nullable=False)
    source_size = db.Column(db.BigInteger, nullable=False)
    source_mime_type = db.Column(db.String(100), nullable=False)
    source_width = db.Column(db.Integer, nullable=False)
    source_height = db.Column(db.Integer, nullable=False)

    vector = db.Column(Vector(1024), nullable=False)
    embedding_model = db.Column(db.String(128), nullable=False)
    embedding_dimension = db.Column(db.SmallInteger, nullable=False)
    normalization_version = db.Column(db.String(32), nullable=False)

    sort_order = db.Column(
        db.Integer,
        nullable=False,
        default=0,
        comment='商品内图片展示顺序；0 即主图，未归款资产无意义',
    )
    status = db.Column(db.String(20), nullable=False, default='active')
    archived_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.now,
        onupdate=datetime.now,
    )

    product = db.relationship(
        'Product',
        backref=db.backref('image_assets', lazy=True, passive_deletes=True),
    )

    __table_args__ = (
        db.UniqueConstraint(
            'source_provider',
            'source_bucket',
            'source_relative_path',
            'source_revision',
            name='uq_image_assets_source_identity',
        ),
        db.CheckConstraint(
            "status IN ('active', 'archived')",
            name='ck_image_assets_status',
        ),
        db.CheckConstraint(
            'source_revision >= 1',
            name='ck_image_assets_source_revision',
        ),
        db.CheckConstraint(
            'embedding_dimension = 1024',
            name='ck_image_assets_embedding_dimension',
        ),
        db.Index('idx_image_assets_content_hash', 'content_hash'),
        db.Index('idx_image_assets_model_number', 'model_number'),
        db.Index('idx_image_assets_status', 'status'),
        db.Index(
            'idx_image_assets_vector_active_hnsw',
            'vector',
            postgresql_using='hnsw',
            postgresql_ops={'vector': 'vector_cosine_ops'},
            postgresql_with={'m': 16, 'ef_construction': 64},
            postgresql_where=text("status = 'active'"),
        ),
    )

    def to_dict(self):
        """返回稳定的图片资产表示；不暴露临时路径或签名 URL。"""
        return {
            'asset_id': str(self.id),
            'model_number': self.model_number,
            'source_provider': self.source_provider,
            'source_bucket': self.source_bucket,
            'source_relative_path': self.source_relative_path,
            'source_revision': self.source_revision,
            'oss_path': self.oss_path,
            'preview_oss_path': self.preview_oss_path,
            'content_hash': self.content_hash,
            'source_size': self.source_size,
            'source_mime_type': self.source_mime_type,
            'source_width': self.source_width,
            'source_height': self.source_height,
            'embedding_model': self.embedding_model,
            'embedding_dimension': self.embedding_dimension,
            'normalization_version': self.normalization_version,
            'status': self.status,
            'sort_order': self.sort_order,
            'archived_at': self.archived_at.isoformat() if self.archived_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            # 兼容旧响应语义：正式图片定位已迁移到 OSS，不保存本机原始路径。
            'original_path': None,
        }
