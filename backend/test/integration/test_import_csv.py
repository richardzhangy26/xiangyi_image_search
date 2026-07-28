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


def test_mid_batch_db_error_keeps_stats_consistent_and_does_not_block_later_batch(app):
    """第一批（前 200 行）里混入 1 行数据库层错误（型号超出 VARCHAR(100)），
    该批必须整体回滚；但不能影响之后独立的第二批（后 50 行）正常入库，
    且 stats 必须精确反映实际入库行数，不能出现"报告成功但库里是空的"。
    """
    client = app.test_client()
    lines = []
    for i in range(250):
        model_number = 'X' * 150 if i == 99 else f'CS-{i:03d}'
        lines.append(f'{model_number},p{i},https://example.com/{i},相机肩带,{i}.50\n')

    response = _upload(client, HEADER + ''.join(lines))

    assert response.status_code == 200
    stats = response.get_json()['stats']

    # 核心不变量：不重复计数、不漏计
    assert stats['total'] == 250
    assert stats['success'] + stats['failed'] + stats['skipped'] == stats['total']

    # 第一批（含超长型号）整批回滚（200 行）；第二批（后 50 行）正常提交
    assert stats['failed'] == 200
    assert stats['success'] == 50

    # 核心不变量：success 必须等于真实入库行数
    assert stats['success'] == Product.query.count()

    # 批次级错误必须是范围表述，不能给一个看似精确、实则错误的行号
    batch_errors = [e for e in stats['errors'] if '整批提交失败' in e]
    assert len(batch_errors) == 1
    assert '~' in batch_errors[0]


def test_db_error_in_final_batch_returns_200_with_stats_not_500(app):
    """出错行落在收尾提交（循环结束后不足 COMMIT_EVERY 的最后一批）时，
    响应必须仍是 200 + {'message','stats'}，不能变成 500 + {'error': ...}——
    旧的逐行提交实现没有这个问题，这是批量提交引入的回归，必须堵住。
    """
    client = app.test_client()
    lines = []
    for i in range(5):
        model_number = 'X' * 150 if i == 2 else f'CS-{i:03d}'
        lines.append(f'{model_number},p{i},https://example.com/{i},相机肩带,{i}.50\n')

    response = _upload(client, HEADER + ''.join(lines))

    assert response.status_code == 200
    body = response.get_json()
    assert 'stats' in body
    assert 'error' not in body

    stats = body['stats']
    assert stats['total'] == 5
    assert stats['success'] + stats['failed'] + stats['skipped'] == stats['total']
    assert stats['success'] == 0
    assert stats['failed'] == 5
    assert stats['success'] == Product.query.count()
