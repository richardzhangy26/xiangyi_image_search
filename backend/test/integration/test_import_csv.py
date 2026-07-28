"""CSV 导入：批量提交、一次性存在性检查、坏行不拖垮整批。"""
import io

from models import Product, db

HEADER = 'model_number,photographer_file,alibaba_product_url,category,price_1688\n'


def _upload(client, body):
    return client.post('/api/products/import-csv', data={
        'csv_file': (io.BytesIO(body.encode('utf-8')), 'p.csv'),
    }, content_type='multipart/form-data')


def test_imports_all_valid_rows(app):
    client = app.test_client()
    rows = ''.join(
        f'CS-{i:03d},p{i},https://example.com/{i},相机肩带,{i}.50\n' for i in range(250)
    )

    response = _upload(client, HEADER + rows)

    assert response.status_code == 200
    stats = response.get_json()['stats']
    assert stats['total'] == 250
    assert stats['success'] == 250
    assert stats['failed'] == 0
    assert Product.query.count() == 250
    assert float(Product.query.get('CS-007').price_1688) == 7.50


def test_skips_existing_model_numbers(app):
    client = app.test_client()
    _upload(client, HEADER + 'CS-001,p,https://example.com/1,相机肩带,1.00\n')

    response = _upload(client, HEADER + 'CS-001,p,https://example.com/1,相机肩带,1.00\n'
                                        'CS-002,p,https://example.com/2,相机挂绳,2.00\n')

    stats = response.get_json()['stats']
    assert stats['skipped'] == 1
    assert stats['success'] == 1
    assert Product.query.count() == 2


def test_bad_row_does_not_block_good_rows(app):
    client = app.test_client()
    body = (HEADER
            + 'CS-001,p,https://example.com/1,相机肩带,1.00\n'
            + ',p,https://example.com/2,相机挂绳,2.00\n'          # 缺 model_number
            + 'CS-003,p,https://example.com/3,相机肩带,3.00\n')

    response = _upload(client, body)

    stats = response.get_json()['stats']
    assert stats['success'] == 2
    assert stats['failed'] == 1
    assert len(stats['errors']) == 1
    assert '第3行' in stats['errors'][0]
    assert {p.model_number for p in Product.query.all()} == {'CS-001', 'CS-003'}


def test_duplicate_model_number_within_same_csv_counted_once(app):
    client = app.test_client()
    body = (HEADER
            + 'CS-001,p,https://example.com/1,相机肩带,1.00\n'
            + 'CS-001,p,https://example.com/1,相机肩带,9.00\n')

    response = _upload(client, body)

    stats = response.get_json()['stats']
    assert stats['success'] == 1
    assert stats['skipped'] == 1
    assert Product.query.count() == 1


def test_gbk_encoded_csv_is_decoded(app):
    client = app.test_client()
    body = HEADER + 'CS-001,摄影师甲,https://example.com/1,相机肩带,1.00\n'

    response = client.post('/api/products/import-csv', data={
        'csv_file': (io.BytesIO(body.encode('gbk')), 'p.csv'),
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    assert Product.query.get('CS-001').photographer_file == '摄影师甲'
