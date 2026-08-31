"""Issue #27 fence epoch contracts on real PostgreSQL."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from models import PurgeObjectFence, db


def _fence(*, state: str):
    now = datetime.now()
    return PurgeObjectFence(
        id=uuid.uuid4(),
        formal_bucket='formal-test-bucket',
        formal_key='preview/shared.jpg',
        kind='search_preview',
        batch_id=uuid.uuid4(),
        target_asset_id=uuid.uuid4(),
        state=state,
        acquired_at=now,
        released_at=now if state == 'released' else None,
        audit_retain_until=now + timedelta(days=365),
    )


def test_postgres_allows_released_fence_history_but_one_held_epoch(app):
    db.session.add_all([_fence(state='released'), _fence(state='released')])
    db.session.commit()

    db.session.add(_fence(state='held'))
    db.session.commit()

    db.session.add(_fence(state='held'))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()

    assert PurgeObjectFence.query.filter_by(
        formal_bucket='formal-test-bucket', formal_key='preview/shared.jpg'
    ).count() == 3
