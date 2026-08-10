import re
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from models import AssetActivityRecord, ImageAsset, Product, db
from services.asset_display_name import rename_image_asset


def _product(model_number='CS-001'):
    return Product(
        model_number=model_number,
        photographer_file='摄影师文件',
        alibaba_product_url='https://example.com/product',
        category='挂绳',
    )


def _asset(path, *, model_number=None, status='active', display_name=None):
    digest = uuid.uuid5(uuid.NAMESPACE_URL, path).hex.ljust(64, '0')
    return ImageAsset(
        model_number=model_number,
        source_provider='qiniu-kodo',
        source_bucket='xiangxipackage',
        source_relative_path=path,
        source_revision=1,
        display_name=display_name or path.rsplit('/', 1)[-1],
        oss_path=f'image-search/xiangxipackage/{path}',
        preview_oss_path=(
            f'image-search/previews/preview-v1/{digest[:2]}/{digest}.jpg'
        ),
        content_hash=digest,
        source_size=4096,
        source_mime_type='image/png',
        source_width=1200,
        source_height=800,
        vector=[0.1] * 1024,
        embedding_model='tongyi-embedding-vision-plus-2026-03-06',
        embedding_dimension=1024,
        normalization_version='preview-v1',
        status=status,
    )


def test_lists_only_active_unassigned_assets_with_safe_fields(app):
    db.session.add(_product())
    db.session.add_all([
        _asset('中文 目录/待归款.png'),
        _asset('已有型号/图片.png', model_number='CS-001'),
        _asset('归档/图片.png', status='archived'),
    ])
    db.session.commit()

    response = app.test_client().get('/api/image-assets')

    assert response.status_code == 200
    body = response.get_json()
    assert body['total'] == 1
    assert body['page'] == 1
    assert body['per_page'] == 24
    assert [item['source_relative_path'] for item in body['assets']] == [
        '中文 目录/待归款.png'
    ]
    item = body['assets'][0]
    assert set(item) == {
        'asset_id', 'model_number', 'display_name', 'source_relative_path',
        'version', 'status', 'preview_url',
        'source_size', 'source_mime_type', 'source_width', 'source_height',
        'created_at',
    }
    assert item['preview_url'] == (
        f"/api/image-assets/{item['asset_id']}/preview"
    )
    private_fields = {
        'oss_path', 'preview_oss_path', 'source_bucket', 'content_hash'
    }
    assert not private_fields & set(item)


def test_filters_assignment_search_and_paginates(app):
    db.session.add(_product())
    db.session.add_all([
        _asset('中文 空格/第一页.png'),
        _asset('中文 空格/第二页.png'),
        _asset('其他/不匹配.png'),
        _asset('已归款/匹配.png', model_number='CS-001'),
    ])
    db.session.commit()

    client = app.test_client()
    unassigned = client.get(
        '/api/image-assets?assignment=unassigned&search=中文 空格&page=1&per_page=1'
    ).get_json()
    assigned = client.get(
        '/api/image-assets?assignment=assigned&search=匹配'
    ).get_json()
    all_assets = client.get('/api/image-assets?assignment=all').get_json()

    assert unassigned['total'] == 2
    assert len(unassigned['assets']) == 1
    assert assigned['total'] == 1
    assert assigned['assets'][0]['model_number'] == 'CS-001'
    assert all_assets['total'] == 4


def test_search_matches_display_name_or_immutable_source_path(app):
    db.session.add_all([
        _asset('来源目录/alpha.png', display_name='业务中文名.png'),
        _asset('特殊来源/beta.png', display_name='普通名称.png'),
    ])
    db.session.commit()

    client = app.test_client()
    display_match = client.get(
        '/api/image-assets?search=业务中文名'
    ).get_json()
    source_match = client.get(
        '/api/image-assets?search=特殊来源'
    ).get_json()

    assert [item['display_name'] for item in display_match['assets']] == [
        '业务中文名.png'
    ]
    assert [item['source_relative_path'] for item in source_match['assets']] == [
        '特殊来源/beta.png'
    ]


def test_search_treats_percent_and_underscore_as_literal_text(app):
    db.session.add_all([
        _asset('来源/普通.png', display_name='折扣_100%.png'),
        _asset('来源/不应通配.png', display_name='折扣X100Y.png'),
    ])
    db.session.commit()

    body = app.test_client().get(
        '/api/image-assets?search=折扣_100%'
    ).get_json()

    assert [item['display_name'] for item in body['assets']] == [
        '折扣_100%.png'
    ]


def test_renames_assigned_asset_with_optimistic_version_and_activity(app):
    product = _product()
    asset = _asset('产品图/原名.PNG', model_number=product.model_number)
    db.session.add_all([product, asset])
    db.session.commit()

    response = app.test_client().post(
        f'/api/image-assets/{asset.id}/rename',
        json={'name_body': '客户展示名', 'expected_version': 1},
        headers={'X-Request-ID': 'issue-15-test'},
    )

    assert response.status_code == 200
    renamed = response.get_json()['asset']
    assert renamed['display_name'] == '客户展示名.PNG'
    assert renamed['source_relative_path'] == '产品图/原名.PNG'
    assert renamed['version'] == 2
    activity = AssetActivityRecord.query.one()
    assert activity.target_id == str(asset.id)
    assert activity.request_id == 'issue-15-test'
    assert activity.before_state['display_name'] == '原名.PNG'
    assert activity.after_state['display_name'] == '客户展示名.PNG'
    assert activity.after_state['version'] == 2


def test_stale_rename_returns_latest_without_overwriting(app):
    asset = _asset('待归款/原名.png')
    db.session.add(asset)
    db.session.commit()
    client = app.test_client()

    first = client.post(f'/api/image-assets/{asset.id}/rename', json={
        'name_body': '第一个名称', 'expected_version': 1,
    })
    stale = client.post(f'/api/image-assets/{asset.id}/rename', json={
        'name_body': '过期名称', 'expected_version': 1,
    })

    assert first.status_code == 200
    assert stale.status_code == 409
    assert stale.get_json()['error_code'] == 'IMAGE_ASSET_VERSION_CONFLICT'
    assert stale.get_json()['latest']['display_name'] == '第一个名称.png'
    assert stale.get_json()['latest']['version'] == 2
    db.session.refresh(asset)
    assert asset.display_name == '第一个名称.png'
    assert AssetActivityRecord.query.count() == 1


def test_two_connections_competing_on_one_version_have_one_winner(app):
    """Requires explicitly authorized isolated PostgreSQL execution."""
    asset = _asset('并发/原名.png')
    db.session.add(asset)
    db.session.commit()
    asset_id = asset.id
    schema_name = db.session.execute(text('SELECT current_schema()')).scalar_one()
    assert re.fullmatch(r'[a-z0-9_]+', schema_name)

    engine = create_engine(app.config['SQLALCHEMY_DATABASE_URI'])
    Session = sessionmaker(bind=engine)
    barrier = threading.Barrier(2)
    outcomes = []
    outcome_lock = threading.Lock()

    def compete(name_body):
        session = Session()
        try:
            session.execute(text(
                f'SET search_path TO "{schema_name}", public'
            ))
            barrier.wait(timeout=5)
            outcome = rename_image_asset(
                session,
                asset_id,
                name_body=name_body,
                expected_version=1,
                request_id=f'concurrent-{name_body}',
            )
            with outcome_lock:
                outcomes.append(outcome)
        finally:
            session.close()

    threads = [
        threading.Thread(target=compete, args=('并发甲',)),
        threading.Thread(target=compete, args=('并发乙',)),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
    finally:
        engine.dispose()

    assert sorted(outcome.status for outcome in outcomes) == [
        'conflict', 'renamed'
    ]
    db.session.expire_all()
    assert db.session.get(ImageAsset, asset_id).version == 2
    assert AssetActivityRecord.query.filter_by(
        target_id=str(asset_id), event_type='asset.rename'
    ).count() == 1


@pytest.mark.parametrize('name_body', [
    '', '   ', '.', '..', '有/斜杠', '有\\反斜杠', '控制\x00字符', 'x' * 101,
])
def test_rename_rejects_invalid_name_body_without_writing(app, name_body):
    asset = _asset('待归款/原名.png')
    db.session.add(asset)
    db.session.commit()

    response = app.test_client().post(
        f'/api/image-assets/{asset.id}/rename',
        json={'name_body': name_body, 'expected_version': 1},
    )

    assert response.status_code == 400
    db.session.refresh(asset)
    assert asset.display_name == '原名.png'
    assert asset.version == 1
    assert AssetActivityRecord.query.count() == 0


def test_rename_allows_duplicate_display_names(app):
    first = _asset('目录一/a.png')
    second = _asset('目录二/b.png')
    db.session.add_all([first, second])
    db.session.commit()
    client = app.test_client()

    first_response = client.post(f'/api/image-assets/{first.id}/rename', json={
        'name_body': '相同业务名', 'expected_version': 1,
    })
    second_response = client.post(f'/api/image-assets/{second.id}/rename', json={
        'name_body': '相同业务名', 'expected_version': 1,
    })

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert ImageAsset.query.filter_by(display_name='相同业务名.png').count() == 2


def test_archived_asset_cannot_be_renamed(app):
    asset = _asset('归档/原名.png', status='archived')
    db.session.add(asset)
    db.session.commit()

    response = app.test_client().post(
        f'/api/image-assets/{asset.id}/rename',
        json={'name_body': '新名称', 'expected_version': 1},
    )

    assert response.status_code == 409
    assert response.get_json()['error_code'] == 'IMAGE_ASSET_NOT_ACTIVE'


def test_rejects_invalid_management_list_parameters(app):
    client = app.test_client()
    assert client.get('/api/image-assets?assignment=unknown').status_code == 400
    assert client.get('/api/image-assets?page=0').status_code == 400
    assert client.get('/api/image-assets?per_page=101').status_code == 400


def test_assigns_multiple_unassigned_assets_in_one_transaction(app):
    product = _product()
    first = _asset('待归款/一.png')
    second = _asset('待归款/二.png')
    db.session.add_all([product, first, second])
    db.session.commit()

    response = app.test_client().post('/api/image-assets/assign', json={
        'asset_ids': [str(first.id), str(second.id)],
        'model_number': product.model_number,
    })

    assert response.status_code == 200
    assert response.get_json() == {
        'model_number': 'CS-001', 'assigned_count': 2, 'reused_count': 0,
    }
    db.session.expire_all()
    assert db.session.get(ImageAsset, first.id).model_number == 'CS-001'
    assert db.session.get(ImageAsset, second.id).model_number == 'CS-001'


def test_assignment_is_idempotent_for_the_same_model(app):
    product = _product()
    asset = _asset('已归款/图片.png', model_number='CS-001')
    db.session.add_all([product, asset])
    db.session.commit()

    response = app.test_client().post('/api/image-assets/assign', json={
        'asset_ids': [str(asset.id)], 'model_number': 'CS-001',
    })

    assert response.status_code == 200
    assert response.get_json()['assigned_count'] == 0
    assert response.get_json()['reused_count'] == 1


def test_assignment_conflict_rolls_back_the_whole_batch(app):
    first_product = _product('CS-001')
    second_product = _product('CS-002')
    free_asset = _asset('待归款/保持未归款.png')
    conflict = _asset('已归款/冲突.png', model_number='CS-002')
    db.session.add_all([first_product, second_product, free_asset, conflict])
    db.session.commit()

    response = app.test_client().post('/api/image-assets/assign', json={
        'asset_ids': [str(free_asset.id), str(conflict.id)],
        'model_number': 'CS-001',
    })

    assert response.status_code == 409
    assert response.get_json()['error_code'] == 'IMAGE_ASSET_ASSIGNMENT_CONFLICT'
    db.session.expire_all()
    assert db.session.get(ImageAsset, free_asset.id).model_number is None
    assert db.session.get(ImageAsset, conflict.id).model_number == 'CS-002'


def test_assignment_rejects_missing_product_asset_and_archived_asset(app):
    product = _product()
    archived = _asset('归档/图片.png', status='archived')
    db.session.add_all([product, archived])
    db.session.commit()
    client = app.test_client()

    missing_product = client.post('/api/image-assets/assign', json={
        'asset_ids': [str(archived.id)], 'model_number': 'NOT-FOUND',
    })
    missing_asset = client.post('/api/image-assets/assign', json={
        'asset_ids': [str(uuid.uuid4())], 'model_number': 'CS-001',
    })
    archived_asset = client.post('/api/image-assets/assign', json={
        'asset_ids': [str(archived.id)], 'model_number': 'CS-001',
    })

    assert missing_product.status_code == 404
    assert missing_product.get_json()['error_code'] == 'PRODUCT_NOT_FOUND'
    assert missing_asset.status_code == 404
    assert missing_asset.get_json()['error_code'] == 'IMAGE_ASSET_NOT_FOUND'
    assert archived_asset.status_code == 409
    assert archived_asset.get_json()['error_code'] == 'IMAGE_ASSET_NOT_ACTIVE'


def test_assignment_rejects_invalid_or_duplicate_asset_ids(app):
    client = app.test_client()
    asset_id = str(uuid.uuid4())

    assert client.post('/api/image-assets/assign', json={}).status_code == 400
    assert client.post('/api/image-assets/assign', json={
        'asset_ids': [asset_id, asset_id], 'model_number': 'CS-001',
    }).status_code == 400
    assert client.post('/api/image-assets/assign', json={
        'asset_ids': ['not-a-uuid'], 'model_number': 'CS-001',
    }).status_code == 400
