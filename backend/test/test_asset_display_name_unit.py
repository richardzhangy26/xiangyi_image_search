"""Pure unit tests for asset display names; never create an app or DB engine."""

import uuid
from types import SimpleNamespace

import pytest

from services.asset_display_name import (
    DisplayNameValidationError,
    compose_display_name,
    default_display_name,
    normalize_name_body,
    rename_image_asset,
)


@pytest.mark.parametrize(
    ('source_path', 'expected'),
    [
        ('中文 目录/夏季.蓝色.PNG', '夏季.蓝色.PNG'),
        ('single.jpg', 'single.jpg'),
        ('nested/path/no-extension', 'no-extension'),
    ],
)
def test_default_display_name_uses_the_source_basename(source_path, expected):
    assert default_display_name(source_path) == expected


def test_compose_display_name_trims_body_and_preserves_source_extension():
    assert compose_display_name('目录/旧名称.JpG', ' 新名称 ') == '新名称.JpG'


@pytest.mark.parametrize('length', [1, 100])
def test_name_body_accepts_the_inclusive_length_boundaries(length):
    assert normalize_name_body('名' * length) == '名' * length


@pytest.mark.parametrize(
    'value',
    [None, '', ' ', '名' * 101, '.', '..', '目录/名称', '目录\\名称', '坏\n名称'],
)
def test_name_body_rejects_invalid_values(value):
    with pytest.raises(DisplayNameValidationError):
        normalize_name_body(value)


def _asset(*, version=1, status='active', display_name='旧名称.JPG'):
    return SimpleNamespace(
        id=uuid.uuid4(),
        model_number=None,
        display_name=display_name,
        source_relative_path='目录/旧名称.JPG',
        version=version,
        status=status,
        source_size=42,
        source_mime_type='image/jpeg',
        source_width=20,
        source_height=10,
        created_at=None,
    )


class _Mappings:
    def __init__(self, value):
        self.value = value

    def one_or_none(self):
        return self.value


class _ExecuteResult:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return _Mappings(self.value)


class FakeSession:
    def __init__(self, reads, update_result=None, add_error=None):
        self.reads = list(reads)
        self.update_result = update_result
        self.add_error = add_error
        self.executed = []
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.expired = 0

    def get(self, _model, _asset_id):
        return self.reads.pop(0) if self.reads else None

    def execute(self, statement):
        self.executed.append(statement)
        return _ExecuteResult(self.update_result)

    def add(self, value):
        if self.add_error:
            raise self.add_error
        self.added.append(value)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def expire_all(self):
        self.expired += 1


def _updated_mapping(asset, *, display_name='新名称.JPG', version=2):
    return {
        'id': asset.id,
        'model_number': asset.model_number,
        'display_name': display_name,
        'source_relative_path': asset.source_relative_path,
        'version': version,
        'status': asset.status,
        'source_size': asset.source_size,
        'source_mime_type': asset.source_mime_type,
        'source_width': asset.source_width,
        'source_height': asset.source_height,
        'created_at': asset.created_at,
    }


def test_rename_updates_version_and_commits_one_activity_record():
    current = _asset()
    session = FakeSession([current], _updated_mapping(current))

    result = rename_image_asset(
        session,
        current.id,
        name_body=' 新名称 ',
        expected_version=1,
        request_id='request-1',
    )

    assert result.status == 'renamed'
    assert result.asset['display_name'] == '新名称.JPG'
    assert result.asset['version'] == 2
    assert len(session.executed) == 1
    assert session.commits == 1
    assert session.rollbacks == 0
    assert len(session.added) == 1
    activity = session.added[0]
    assert activity.event_type == 'asset.rename'
    assert activity.before_state == {
        'model_number': None, 'display_name': '旧名称.JPG',
        'version': 1, 'status': 'active'
    }
    assert activity.after_state == {
        'model_number': None, 'display_name': '新名称.JPG',
        'version': 2, 'status': 'active'
    }


def test_stale_version_returns_latest_without_writing():
    current = _asset(version=3, display_name='服务器最新.JPG')
    session = FakeSession([current])

    result = rename_image_asset(
        session,
        current.id,
        name_body='用户草稿',
        expected_version=2,
        request_id='request-2',
    )

    assert result.status == 'conflict'
    assert result.error_code == 'IMAGE_ASSET_VERSION_CONFLICT'
    assert result.asset['display_name'] == '服务器最新.JPG'
    assert result.asset['version'] == 3
    assert session.executed == []
    assert session.commits == 0


def test_update_race_returns_the_latest_archived_representation():
    current = _asset()
    archived = _asset(version=2, status='archived')
    archived.id = current.id
    session = FakeSession([current, archived], update_result=None)

    result = rename_image_asset(
        session,
        current.id,
        name_body='用户草稿',
        expected_version=1,
        request_id='request-3',
    )

    assert result.status == 'not_active'
    assert result.error_code == 'IMAGE_ASSET_NOT_ACTIVE'
    assert result.asset['status'] == 'archived'
    assert session.expired == 1


def test_activity_failure_rolls_back_the_asset_update():
    current = _asset()
    session = FakeSession(
        [current],
        _updated_mapping(current),
        add_error=RuntimeError('activity insert failed'),
    )

    with pytest.raises(RuntimeError, match='activity insert failed'):
        rename_image_asset(
            session,
            current.id,
            name_body='新名称',
            expected_version=1,
            request_id='request-4',
        )

    assert session.commits == 0
    assert session.rollbacks == 1
