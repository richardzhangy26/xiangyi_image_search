"""Append-only, safe Issue #27 item operation evidence."""

import uuid
from datetime import datetime

from sqlalchemy import Uuid

from . import db


class PurgeItemEvent(db.Model):
    __tablename__ = 'purge_item_events'

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id = db.Column(Uuid(as_uuid=True), nullable=False)
    target_asset_id = db.Column(Uuid(as_uuid=True), nullable=False)
    event_type = db.Column(db.String(80), nullable=False)
    result_code = db.Column(db.String(80), nullable=True)
    error_code = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    audit_retain_until = db.Column(db.DateTime, nullable=False)
