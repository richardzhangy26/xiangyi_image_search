"""Pure unit contracts for the Issue #17 recycle-bin service seam."""

from datetime import datetime
import importlib
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy.dialects import postgresql


FIXED_ARCHIVE_TIME = datetime(2026, 8, 9, 12, 0, 0)
FIXED_CREATED_TIME = datetime(2026, 8, 1, 8, 30, 0)


def _recycle_bin_module():
    """Import inside a test so a missing RED module is a test failure."""
    try:
        return importlib.import_module('services.asset_recycle_bin')
    except ModuleNotFoundError as exc:
        if exc.name == 'services.asset_recycle_bin':
            pytest.fail(
                'Issue #17 RED: services.asset_recycle_bin 尚未实现',
                pytrace=False,
            )
        raise


def _asset(
    *,
    asset_id=None,
    status='archived',
    version=1,
    archived_at=FIXED_ARCHIVE_TIME,
    model_number=None,
    display_name='蓝色挂绳.png',
    source_relative_path='挂绳/A47/2.png',
    **extra,
):
    values = {
        'id': asset_id or uuid.uuid4(),
        'status': status,
        'version': version,
        'archived_at': archived_at,
        'model_number': model_number,
        'display_name': display_name,
        'source_relative_path': source_relative_path,
        'source_size': 4096,
        'source_mime_type': 'image/png',
        'source_width': 1200,
        'source_height': 800,
        'created_at': FIXED_CREATED_TIME,
    }
    values.update(extra)
    return SimpleNamespace(**values)


def _updated(asset, *, version):
    return {
        'id': asset.id,
        'model_number': asset.model_number,
        'display_name': asset.display_name,
        'version': version,
        'status': 'active',
    }


class _Scalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _Mappings:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _Result:
    def __init__(self, *, scalar=None, rows=None, mappings=None):
        self.scalar_value = scalar
        self.rows = rows or []
        self.mapping_values = mappings or []

    def scalar(self):
        return self.scalar_value

    def scalar_one(self):
        return self.scalar_value

    def scalars(self):
        return _Scalars(self.rows)

    def mappings(self):
        return _Mappings(self.mapping_values)


def _compiled_sql(statement):
    return ' '.join(str(statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={'literal_binds': True},
    )).split())


class FakeListSession:
    def __init__(self, *, assets, total, archived_total):
        self.assets = assets
        self.total = total
        self.archived_total = archived_total
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement):
        self.executed.append(statement)
        sql = _compiled_sql(statement).lower()
        if 'count(' in sql:
            count = self.total if ' ilike ' in sql else self.archived_total
            return _Result(scalar=count)
        return _Result(rows=self.assets)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeRestoreSession:
    def __init__(
        self,
        *,
        locked,
        updated,
        execute_errors=None,
        add_all_error=None,
        commit_error=None,
    ):
        self.locked = locked
        self.updated = updated
        self.execute_errors = execute_errors or {}
        self.add_all_error = add_all_error
        self.commit_error = commit_error
        self.executed = []
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement):
        self.executed.append(statement)
        call_number = len(self.executed)
        if call_number in self.execute_errors:
            raise self.execute_errors[call_number]
        sql = _compiled_sql(statement).lower()
        if sql.startswith('select '):
            return _Result(rows=self.locked)
        return _Result(mappings=self.updated)

    def add_all(self, records):
        if self.add_all_error:
            raise self.add_all_error
        self.added.extend(records)

    def commit(self):
        self.commits += 1
        if self.commit_error:
            raise self.commit_error

    def rollback(self):
        self.rollbacks += 1


def test_lists_archived_assets_with_dual_counts_safe_representation_and_no_commit():
    module = _recycle_bin_module()
    asset = _asset(
        display_name='A%_\\ 蓝色挂绳.png',
        source_relative_path='特殊/A%_\\/2.png',
    )
    session = FakeListSession(assets=[asset], total=1, archived_total=7)

    page = module.list_archived_image_assets(
        session,
        page=2,
        per_page=24,
        search=' A%_\\ ',
    )

    assert page.total == 1
    assert page.archived_total == 7
    assert page.page == 2
    assert page.per_page == 24
    assert len(page.assets) == 1
    representation = page.assets[0]
    assert representation['archived_at'] == '2026-08-09T12:00:00'
    assert representation['preview_url'] == (
        f'/api/image-assets/{asset.id}/preview'
    )
    assert set(representation) == {
        'asset_id', 'model_number', 'display_name', 'source_relative_path',
        'version', 'status', 'archived_at', 'preview_url', 'source_size',
        'source_mime_type', 'source_width', 'source_height', 'created_at',
    }
    assert {
        'vector', 'oss_path', 'preview_oss_path', 'content_hash',
        'source_bucket',
    }.isdisjoint(representation)
    assert session.commits == 0

    compiled = [_compiled_sql(statement) for statement in session.executed]
    count_sql = [sql for sql in compiled if 'count(' in sql.lower()]
    list_sql = [sql for sql in compiled if 'count(' not in sql.lower()]
    assert len(count_sql) == 2
    assert len(list_sql) == 1
    assert all("image_assets.status = 'archived'" in sql for sql in compiled)
    assert 'image_assets.display_name ILIKE' in list_sql[0]
    assert 'image_assets.source_relative_path ILIKE' in list_sql[0]
    assert 'image_assets.archived_at DESC NULLS LAST' in list_sql[0]
    assert 'image_assets.id DESC' in list_sql[0]
    assert 'LIMIT 24 OFFSET 24' in list_sql[0]
    search_parameters = {
        value
        for statement in session.executed
        for value in statement.compile().params.values()
        if isinstance(value, str) and 'A' in value
    }
    assert '%A\\%\\_\\\\%' in search_parameters


def test_restores_archived_and_keeps_active_retry_idempotent():
    module = _recycle_bin_module()
    active = _asset(
        asset_id=uuid.UUID(int=1),
        status='active',
        version=7,
        archived_at=None,
    )
    archived = _asset(asset_id=uuid.UUID(int=2), version=3)
    session = FakeRestoreSession(
        locked=[active, archived],
        updated=[_updated(archived, version=4)],
    )

    result = module.restore_image_assets(
        session,
        [str(archived.id), str(active.id)],
        request_id='issue-17-mixed',
    )

    assert result.status == 'succeeded'
    assert result.restored_count == 1
    assert result.already_active_count == 1
    assert [item.asset_id for item in result.items] == [
        str(archived.id), str(active.id),
    ]
    assert [item.status for item in result.items] == [
        'restored', 'already_active',
    ]
    assert [item.version for item in result.items] == [4, 7]
    assert session.commits == 1
    assert session.rollbacks == 0
    assert len(session.added) == 3
    assert session.added[0].event_type == 'asset.restore.batch'
    assert [record.event_type for record in session.added[1:]] == [
        'asset.restore', 'asset.restore',
    ]
    assert session.added[2].result == 'noop'
    assert session.added[2].before_state == session.added[2].after_state
    assert session.added[1].before_state['status'] == 'archived'
    assert session.added[1].after_state == {
        'model_number': None,
        'display_name': archived.display_name,
        'version': 4,
        'status': 'active',
    }
    lock_sql, update_sql = map(_compiled_sql, session.executed)
    assert 'ORDER BY image_assets.id FOR UPDATE' in lock_sql
    assert "image_assets.status = 'archived'" in update_sql
    assert 'image_assets.model_number IS NULL' in update_sql


def test_active_assigned_retry_is_noop_and_preserves_assignment():
    module = _recycle_bin_module()
    active_assigned = _asset(
        status='active',
        archived_at=None,
        model_number='CS-001',
        version=9,
    )
    session = FakeRestoreSession(locked=[active_assigned], updated=[])

    result = module.restore_image_assets(
        session,
        [str(active_assigned.id)],
        request_id='issue-17-active-assigned',
    )

    assert result.status == 'succeeded'
    assert result.restored_count == 0
    assert result.already_active_count == 1
    assert result.items[0].status == 'already_active'
    assert result.items[0].version == 9
    assert active_assigned.model_number == 'CS-001'
    assert active_assigned.version == 9
    assert session.commits == 1
    assert session.rollbacks == 0
    assert len(session.added) == 2
    item_activity = session.added[1]
    assert item_activity.result == 'noop'
    assert item_activity.before_state == item_activity.after_state
    assert item_activity.after_state['model_number'] == 'CS-001'


@pytest.mark.parametrize(
    ('conflict', 'expected_error_code'),
    [
        ('missing', 'IMAGE_ASSET_NOT_FOUND'),
        ('duplicate', 'IMAGE_ASSET_DUPLICATE_TARGET'),
        ('invalid_status', 'IMAGE_ASSET_INVALID_STATUS'),
        ('archived_assigned', 'IMAGE_ASSET_ALREADY_ASSIGNED'),
    ],
)
def test_missing_duplicate_invalid_or_archived_assigned_rejects_all(
    conflict,
    expected_error_code,
):
    module = _recycle_bin_module()
    eligible = _asset(asset_id=uuid.UUID(int=10), version=4)
    conflict_id = uuid.UUID(int=11)
    if conflict == 'missing':
        requested = [str(eligible.id), str(conflict_id)]
        locked = [eligible]
    elif conflict == 'duplicate':
        requested = [str(eligible.id), str(eligible.id).upper()]
        locked = [eligible]
    elif conflict == 'invalid_status':
        invalid = _asset(asset_id=conflict_id, status='processing')
        requested = [str(eligible.id), str(invalid.id)]
        locked = [eligible, invalid]
    else:
        assigned = _asset(asset_id=conflict_id, model_number='CS-001')
        requested = [str(eligible.id), str(assigned.id)]
        locked = [eligible, assigned]
    session = FakeRestoreSession(locked=locked, updated=[])

    result = module.restore_image_assets(
        session,
        requested,
        request_id=f'issue-17-{conflict}',
    )

    assert result.status == 'rejected'
    assert result.restored_count == 0
    assert result.already_active_count == 0
    assert len(session.executed) == 1
    assert eligible.status == 'archived'
    assert eligible.version == 4
    assert session.commits == 1
    assert session.rollbacks == 0
    rejected = [item for item in result.items if item.status == 'rejected']
    assert rejected
    assert rejected[-1].error_code == expected_error_code
    assert rejected[-1].error
    if conflict == 'duplicate':
        assert len(result.items) == 1
        assert len(session.added) == 2
    else:
        assert [item.status for item in result.items] == [
            'unchanged', 'rejected',
        ]
        assert len(session.added) == 3
    assert session.added[0].result == 'rejected'
    assert session.added[-1].error_code == expected_error_code


@pytest.mark.parametrize(
    'asset_ids',
    [
        None,
        (),
        [],
        [str(uuid.uuid4()) for _ in range(101)],
        [123],
        ['not-a-uuid'],
    ],
)
def test_rejects_empty_oversized_non_string_and_invalid_uuid_payloads(asset_ids):
    module = _recycle_bin_module()
    session = FakeRestoreSession(locked=[], updated=[])

    with pytest.raises(module.RestoreRequestValidationError) as exc_info:
        module.restore_image_assets(
            session,
            asset_ids,
            request_id='issue-17-invalid-payload',
        )

    assert exc_info.value.error_code == 'INVALID_IMAGE_ASSET_RESTORE_BATCH'
    assert session.executed == []
    assert session.added == []
    assert session.commits == 0
    assert session.rollbacks == 0
    assert 'not-a-uuid' not in str(exc_info.value)


def test_update_count_mismatch_rolls_back_without_activity_commit():
    module = _recycle_bin_module()
    first = _asset(asset_id=uuid.UUID(int=20))
    second = _asset(asset_id=uuid.UUID(int=21))
    session = FakeRestoreSession(
        locked=[first, second],
        updated=[_updated(first, version=2)],
    )

    with pytest.raises(RuntimeError, match='restore update count mismatch'):
        module.restore_image_assets(
            session,
            [str(first.id), str(second.id)],
            request_id='issue-17-update-mismatch',
        )

    assert session.commits == 0
    assert session.rollbacks == 1
    assert session.added == []


@pytest.mark.parametrize(
    ('add_all_error', 'commit_error', 'message', 'commit_attempts'),
    [
        (RuntimeError('activity insert failed'), None, 'activity insert failed', 0),
        (None, RuntimeError('commit failed'), 'commit failed', 1),
    ],
)
def test_activity_or_commit_failure_rolls_back_every_restore(
    add_all_error,
    commit_error,
    message,
    commit_attempts,
):
    module = _recycle_bin_module()
    asset = _asset(version=6)
    session = FakeRestoreSession(
        locked=[asset],
        updated=[_updated(asset, version=7)],
        add_all_error=add_all_error,
        commit_error=commit_error,
    )

    with pytest.raises(RuntimeError, match=message):
        module.restore_image_assets(
            session,
            [str(asset.id)],
            request_id='issue-17-transaction-failure',
        )

    assert session.commits == commit_attempts
    assert session.rollbacks == 1
    assert asset.status == 'archived'
    assert asset.version == 6


def test_lock_failure_rolls_back_without_activity_or_commit():
    module = _recycle_bin_module()
    session = FakeRestoreSession(
        locked=[],
        updated=[],
        execute_errors={1: RuntimeError('lock failed')},
    )

    with pytest.raises(RuntimeError, match='lock failed'):
        module.restore_image_assets(
            session,
            [str(uuid.uuid4())],
            request_id='issue-17-lock-failure',
        )

    assert session.commits == 0
    assert session.rollbacks == 1
    assert session.added == []


def test_restore_activity_never_contains_vector_or_object_fields():
    module = _recycle_bin_module()
    asset = _asset(
        vector='vector-secret',
        oss_path='object-secret',
        preview_oss_path='preview-secret',
        embedding_model='embedding-secret',
        content_hash='hash-secret',
        source_bucket='bucket-secret',
    )
    session = FakeRestoreSession(
        locked=[asset],
        updated=[_updated(asset, version=2)],
    )

    module.restore_image_assets(
        session,
        [str(asset.id)],
        request_id='issue-17-safe-activity',
    )

    item_records = [
        record for record in session.added
        if record.event_type == 'asset.restore'
    ]
    assert len(item_records) == 1
    safe_fields = {'model_number', 'display_name', 'version', 'status'}
    forbidden = {
        'vector', 'oss_path', 'preview_oss_path', 'embedding_model',
        'content_hash', 'source_bucket',
    }
    secrets = {
        'vector-secret', 'object-secret', 'preview-secret',
        'embedding-secret', 'hash-secret', 'bucket-secret',
    }
    for record in item_records:
        for state in (record.before_state, record.after_state):
            assert set(state) == safe_fields
            assert forbidden.isdisjoint(state)
            assert secrets.isdisjoint(state.values())
