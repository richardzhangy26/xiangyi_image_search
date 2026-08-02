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
