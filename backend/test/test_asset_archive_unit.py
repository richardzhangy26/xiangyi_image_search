"""Pure unit tests for the public batch-archive transaction seam."""

from datetime import datetime
from types import SimpleNamespace
import uuid

import pytest

from services.asset_archive import archive_unassigned_image_assets


FIXED_ARCHIVE_TIME = datetime(2026, 8, 9, 12, 0, 0)


def _asset(
    *, status='active', version=1, archived_at=None, model_number=None,
    **extra,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        version=version,
        archived_at=archived_at,
        model_number=model_number,
        display_name='待归档.png',
        **extra,
    )


def _updated(asset, *, version):
    return {
        'id': asset.id,
        'model_number': asset.model_number,
        'display_name': asset.display_name,
        'version': version,
        'status': 'archived',
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
    def __init__(self, *, locked=None, updated=None):
        self.locked = locked
        self.updated = updated

    def scalars(self):
        return _Scalars(self.locked)

    def mappings(self):
        return _Mappings(self.updated)


class FakeSession:
    def __init__(self, *, locked, updated, add_all_error=None, execute_error=None):
        self.locked = locked
        self.updated = updated
        self.executed = []
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.add_all_error = add_all_error
        self.execute_error = execute_error

    def execute(self, statement):
        self.executed.append(statement)
        if self.execute_error:
            raise self.execute_error
        if len(self.executed) == 1:
            return _Result(locked=self.locked)
        return _Result(updated=self.updated)

    def add_all(self, records):
        if self.add_all_error:
            raise self.add_all_error
        self.added.extend(records)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_archives_active_unassigned_and_keeps_archived_retry_unchanged():
    active = _asset(version=3)
    archived = _asset(
        status='archived', version=7, archived_at=FIXED_ARCHIVE_TIME
    )
    session = FakeSession(
        locked=[active, archived],
        updated=[_updated(active, version=4)],
    )

    result = archive_unassigned_image_assets(
        session,
        [str(active.id), str(archived.id)],
        request_id='issue-16-request',
    )

    assert result.status == 'succeeded'
    assert result.archived_count == 1
    assert result.already_archived_count == 1
    assert [item.status for item in result.items] == [
        'archived', 'already_archived'
    ]
    assert result.items[0].version == 4
    assert result.items[1].version == 7
    assert archived.archived_at == FIXED_ARCHIVE_TIME
    assert session.commits == 1
    assert session.rollbacks == 0
    assert len(session.added) == 3
    assert session.added[2].result == 'noop'


def test_rejects_duplicate_uuid_spellings_and_records_unique_item_result():
    asset = _asset()
    session = FakeSession(locked=[asset], updated=[])

    result = archive_unassigned_image_assets(
        session,
        [str(asset.id), str(asset.id).upper()],
        request_id='issue-16-duplicate',
    )

    assert result.status == 'rejected'
    assert result.archived_count == 0
    assert result.already_archived_count == 0
    assert len(session.executed) == 1
    assert session.commits == 1
    assert len(session.added) == 2
    assert result.items == [
        result.items[0]
    ]
    assert result.items[0].asset_id == str(asset.id)
    assert result.items[0].status == 'rejected'
    assert result.items[0].error_code == 'IMAGE_ASSET_DUPLICATE_TARGET'


def test_missing_asset_rejects_the_whole_batch_without_update():
    present = _asset()
    missing = uuid.uuid4()
    session = FakeSession(locked=[present], updated=[])

    result = archive_unassigned_image_assets(
        session, [str(present.id), str(missing)], request_id='missing'
    )

    assert result.status == 'rejected'
    assert session.executed and len(session.executed) == 1
    assert [item.status for item in result.items] == ['unchanged', 'rejected']
    assert result.items[1].error_code == 'IMAGE_ASSET_NOT_FOUND'
    assert present.status == 'active'
    assert present.version == 1
    assert session.commits == 1


def test_assigned_active_asset_rejects_the_whole_batch_without_update():
    eligible = _asset()
    assigned = _asset(model_number='CS-001', version=8)
    session = FakeSession(locked=[eligible, assigned], updated=[])

    result = archive_unassigned_image_assets(
        session, [str(eligible.id), str(assigned.id)], request_id='assigned'
    )

    assert result.status == 'rejected'
    assert len(session.executed) == 1
    assert [item.status for item in result.items] == ['unchanged', 'rejected']
    assert result.items[1].error_code == 'IMAGE_ASSET_ALREADY_ASSIGNED'
    assert (eligible.status, eligible.version) == ('active', 1)
    assert (assigned.status, assigned.version) == ('active', 8)


def test_unknown_or_archived_assigned_state_rejects_the_whole_batch():
    processing = _asset(status='processing')
    archived_assigned = _asset(status='archived', model_number='CS-001')
    session = FakeSession(locked=[processing, archived_assigned], updated=[])

    result = archive_unassigned_image_assets(
        session,
        [str(processing.id), str(archived_assigned.id)],
        request_id='invalid-state',
    )

    assert result.status == 'rejected'
    assert len(session.executed) == 1
    assert [item.error_code for item in result.items] == [
        'IMAGE_ASSET_INVALID_STATUS', 'IMAGE_ASSET_ALREADY_ASSIGNED'
    ]
    assert len(session.added) == 3


def test_rejected_batch_records_each_valid_target_reason():
    eligible = _asset()
    assigned = _asset(model_number='CS-001')
    missing = uuid.uuid4()
    session = FakeSession(locked=[eligible, assigned], updated=[])

    result = archive_unassigned_image_assets(
        session,
        [str(eligible.id), str(missing), str(assigned.id)],
        request_id='rejected-audit',
    )

    assert result.status == 'rejected'
    assert [item.status for item in result.items] == [
        'unchanged', 'rejected', 'rejected'
    ]
    assert len(session.added) == 4
    batch_id = session.added[0].batch_id
    assert all(record.batch_id == batch_id for record in session.added)
    assert session.added[2].error_code == 'IMAGE_ASSET_NOT_FOUND'
    assert session.added[3].error_code == 'IMAGE_ASSET_ALREADY_ASSIGNED'


@pytest.mark.parametrize(
    'asset_ids',
    [[], [str(uuid.uuid4()) for _ in range(101)], [123], ['not-a-uuid']],
)
def test_rejects_empty_oversized_non_string_and_invalid_uuid_payloads(asset_ids):
    session = FakeSession(locked=[], updated=[])

    with pytest.raises(Exception) as exc_info:
        archive_unassigned_image_assets(session, asset_ids, request_id='invalid')

    assert exc_info.value.error_code == 'INVALID_IMAGE_ASSET_ARCHIVE_BATCH'
    assert session.executed == []
    assert session.added == []
    assert session.commits == 0
    assert 'not-a-uuid' not in str(exc_info.value)


def test_update_count_mismatch_rolls_back_without_commit():
    first = _asset()
    second = _asset()
    session = FakeSession(
        locked=[first, second], updated=[_updated(first, version=2)]
    )

    with pytest.raises(RuntimeError, match='archive update count mismatch'):
        archive_unassigned_image_assets(
            session, [str(first.id), str(second.id)], request_id='mismatch'
        )

    assert session.commits == 0
    assert session.rollbacks == 1
    assert session.added == []


def test_activity_failure_rolls_back_the_archive_update():
    asset = _asset()
    session = FakeSession(
        locked=[asset], updated=[_updated(asset, version=2)],
        add_all_error=RuntimeError('activity insert failed'),
    )

    with pytest.raises(RuntimeError, match='activity insert failed'):
        archive_unassigned_image_assets(
            session, [str(asset.id)], request_id='activity-failure'
        )

    assert session.commits == 0
    assert session.rollbacks == 1


def test_lock_failure_rolls_back_before_propagating_the_database_error():
    session = FakeSession(
        locked=[], updated=[], execute_error=RuntimeError('lock failed')
    )

    with pytest.raises(RuntimeError, match='lock failed'):
        archive_unassigned_image_assets(
            session, [str(uuid.uuid4())], request_id='lock-failure'
        )

    assert session.commits == 0
    assert session.rollbacks == 1
    assert session.added == []


def test_activity_states_never_contain_vector_or_object_storage_fields():
    asset = _asset(
        vector='vector-secret', oss_path='object-secret',
        preview_oss_path='preview-secret',
    )
    session = FakeSession(locked=[asset], updated=[_updated(asset, version=2)])

    archive_unassigned_image_assets(session, [str(asset.id)], request_id='safe')

    forbidden = {'vector', 'oss_path', 'preview_oss_path'}
    for record in session.added:
        for state in (record.before_state, record.after_state):
            if state is not None:
                assert forbidden.isdisjoint(state)
                assert 'vector-secret' not in state.values()
                assert 'object-secret' not in state.values()
                assert 'preview-secret' not in state.values()
