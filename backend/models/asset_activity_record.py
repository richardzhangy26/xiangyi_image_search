"""图片资产生命周期动作的持久活动记录。"""

import uuid
from datetime import datetime

from sqlalchemy import Uuid
from sqlalchemy.dialects.postgresql import JSONB

from . import db


# PostgreSQL 使用 JSONB；内存 SQLite（受控单元测试）使用 JSON 变体，
# 使 create_all 可在无真实 PostgreSQL 的环境下建表。
_JSON_STATE = JSONB().with_variant(db.JSON(), 'sqlite')


class AssetActivityRecord(db.Model):
    """保留目标删除后的审计证据，因此不建立级联外键。"""

    __tablename__ = 'asset_activity_records'

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type = db.Column(db.String(64), nullable=False)
    target_type = db.Column(db.String(32), nullable=False)
    target_id = db.Column(db.Text, nullable=False)
    request_id = db.Column(db.String(64), nullable=False)
    source = db.Column(db.String(32), nullable=False)
    actor_id = db.Column(db.Text, nullable=True)
    batch_id = db.Column(db.Text, nullable=True)
    task_id = db.Column(db.Text, nullable=True)
    before_state = db.Column(_JSON_STATE, nullable=True)
    after_state = db.Column(_JSON_STATE, nullable=True)
    result = db.Column(db.String(32), nullable=False)
    error_code = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    __table_args__ = (
        db.Index(
            'idx_asset_activity_target_created',
            'target_type',
            'target_id',
            'created_at',
        ),
        db.Index('idx_asset_activity_request_id', 'request_id'),
    )

