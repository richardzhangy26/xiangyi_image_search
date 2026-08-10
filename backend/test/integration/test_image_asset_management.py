import uuid

from models import ImageAsset, Product, db


def _product(model_number='CS-001'):
    return Product(
        model_number=model_number,
        photographer_file='摄影师文件',
        alibaba_product_url='https://example.com/product',
        category='挂绳',
    )


def _asset(path, *, model_number=None, status='active'):
    digest = uuid.uuid5(uuid.NAMESPACE_URL, path).hex.ljust(64, '0')
    return ImageAsset(
        model_number=model_number,
        source_provider='qiniu-kodo',
        source_bucket='xiangxipackage',
        source_relative_path=path,
        source_revision=1,
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
        'asset_id', 'model_number', 'source_relative_path', 'preview_url',
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
        'product_created': False,
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
    assert client.post('/api/image-assets/assign', json={
        'asset_ids': [asset_id], 'model_number': 'CS-001',
        'create_if_missing': 'yes',
    }).status_code == 400


def test_create_if_missing_creates_product_and_assigns_in_one_transaction(app):
    first = _asset('待归款/新建一.png')
    second = _asset('待归款/新建二.png')
    db.session.add_all([first, second])
    db.session.commit()

    response = app.test_client().post('/api/image-assets/assign', json={
        'asset_ids': [str(first.id), str(second.id)],
        'model_number': 'NEW-001',
        'create_if_missing': True,
    })

    assert response.status_code == 200
    assert response.get_json() == {
        'model_number': 'NEW-001', 'assigned_count': 2, 'reused_count': 0,
        'product_created': True,
    }
    db.session.expire_all()
    product = db.session.get(Product, 'NEW-001')
    assert product is not None
    assert product.photographer_file == ''
    assert product.alibaba_product_url == ''
    assert product.category == ''
    assert db.session.get(ImageAsset, first.id).model_number == 'NEW-001'
    assert db.session.get(ImageAsset, second.id).model_number == 'NEW-001'


def test_missing_product_still_rejected_without_create_if_missing(app):
    asset = _asset('待归款/保持未归款.png')
    db.session.add(asset)
    db.session.commit()

    response = app.test_client().post('/api/image-assets/assign', json={
        'asset_ids': [str(asset.id)], 'model_number': 'NEW-002',
    })

    assert response.status_code == 404
    assert response.get_json()['error_code'] == 'PRODUCT_NOT_FOUND'
    db.session.expire_all()
    assert db.session.get(Product, 'NEW-002') is None
    assert db.session.get(ImageAsset, asset.id).model_number is None


def test_create_if_missing_rejects_oversized_model_number(app):
    asset = _asset('待归款/超长型号.png')
    db.session.add(asset)
    db.session.commit()

    response = app.test_client().post('/api/image-assets/assign', json={
        'asset_ids': [str(asset.id)],
        'model_number': 'X' * 101,
        'create_if_missing': True,
    })

    assert response.status_code == 400
    assert response.get_json()['error_code'] == (
        'INVALID_IMAGE_ASSET_ASSIGNMENT'
    )
    assert db.session.get(Product, 'X' * 101) is None


def test_create_if_missing_conflict_rolls_back_product_and_batch(app):
    other_product = _product('CS-002')
    free_asset = _asset('待归款/新建不回滚.png')
    conflict = _asset('已归款/新建冲突.png', model_number='CS-002')
    db.session.add_all([other_product, free_asset, conflict])
    db.session.commit()

    response = app.test_client().post('/api/image-assets/assign', json={
        'asset_ids': [str(free_asset.id), str(conflict.id)],
        'model_number': 'NEW-003',
        'create_if_missing': True,
    })

    assert response.status_code == 409
    assert response.get_json()['error_code'] == (
        'IMAGE_ASSET_ASSIGNMENT_CONFLICT'
    )
    db.session.expire_all()
    assert db.session.get(Product, 'NEW-003') is None
    assert db.session.get(ImageAsset, free_asset.id).model_number is None
    assert db.session.get(ImageAsset, conflict.id).model_number == 'CS-002'
