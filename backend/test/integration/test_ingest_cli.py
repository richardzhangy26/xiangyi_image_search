"""目录批量导入 CLI：孤儿目录报告、dry-run、幂等。"""
import io
import os

import numpy as np
from PIL import Image

from models import Product, ProductImage, db


def _write_png(path, color):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new('RGB', (8, 8), color).save(path, format='PNG')


class CountingEmbedding:
    def __init__(self):
        self.image_calls = 0

    def embed_image(self, image_path, request_id=None):
        self.image_calls += 1
        return np.full(1024, 0.1, dtype=np.float32)

    def embed_images(self, image_paths, request_id=None):
        self.image_calls += len(image_paths)
        return [np.full(1024, 0.1, dtype=np.float32) for _ in image_paths]


def _add_product(model_number):
    db.session.add(Product(
        model_number=model_number, photographer_file='p',
        alibaba_product_url='https://example.com/x', category='相机肩带',
    ))
    db.session.commit()


def test_scan_directory_maps_dirname_to_model_number(tmp_path):
    from scripts.ingest_images import scan_directory

    _write_png(str(tmp_path / 'CS-001' / '1.png'), 'red')
    _write_png(str(tmp_path / 'CS-001' / '2.png'), 'blue')
    _write_png(str(tmp_path / 'HL-002' / '主图.PNG'), 'green')
    (tmp_path / 'CS-001' / 'notes.txt').write_text('忽略我')
    _write_png(str(tmp_path / '散图.png'), 'black')  # root 下散图不属于任何型号

    scanned = scan_directory(str(tmp_path))

    assert set(scanned) == {'CS-001', 'HL-002'}
    assert [os.path.basename(p) for p in scanned['CS-001']] == ['1.png', '2.png']
    assert [os.path.basename(p) for p in scanned['HL-002']] == ['主图.PNG']


def test_scan_directory_recurses_inside_model_directory(tmp_path):
    from scripts.ingest_images import scan_directory

    _write_png(str(tmp_path / 'CS-001' / '细节图' / 'a.png'), 'red')

    scanned = scan_directory(str(tmp_path))

    assert len(scanned['CS-001']) == 1
    assert scanned['CS-001'][0].endswith(os.path.join('细节图', 'a.png'))


def test_orphan_directories_are_reported_and_skipped(app, tmp_path):
    from scripts.ingest_images import run

    _add_product('CS-001')
    _write_png(str(tmp_path / 'CS-001' / '1.png'), 'red')
    _write_png(str(tmp_path / 'CS-007' / '1.png'), 'blue')
    _write_png(str(tmp_path / 'CS-08' / '1.png'), 'green')

    report = run(app, str(tmp_path), embedding_client=CountingEmbedding())

    assert sorted(report.orphan_dirs) == ['CS-007', 'CS-08']
    assert report.created == 1
    assert ProductImage.query.count() == 1


def test_dry_run_writes_nothing_and_calls_no_api(app, tmp_path):
    from scripts.ingest_images import run

    _add_product('CS-001')
    _write_png(str(tmp_path / 'CS-001' / '1.png'), 'red')
    embedding = CountingEmbedding()

    report = run(app, str(tmp_path), dry_run=True, embedding_client=embedding)

    assert report.created == 1          # 报告「将会入库 1 张」
    assert ProductImage.query.count() == 0
    assert embedding.image_calls == 0


def test_rerun_is_idempotent_with_zero_api_calls(app, tmp_path):
    from scripts.ingest_images import run

    _add_product('CS-001')
    _write_png(str(tmp_path / 'CS-001' / '1.png'), 'red')
    _write_png(str(tmp_path / 'CS-001' / '2.png'), 'blue')

    first_embedding = CountingEmbedding()
    first = run(app, str(tmp_path), embedding_client=first_embedding)
    assert first.created == 2
    assert first_embedding.image_calls == 2

    second_embedding = CountingEmbedding()
    second = run(app, str(tmp_path), embedding_client=second_embedding)

    assert second.created == 0
    assert second.duplicates == 2
    assert second_embedding.image_calls == 0
    assert ProductImage.query.count() == 2


def test_duplicate_across_model_directories_is_reported(app, tmp_path):
    from scripts.ingest_images import run

    _add_product('CS-001')
    _add_product('HL-002')
    _write_png(str(tmp_path / 'CS-001' / '1.png'), 'red')
    # 完全相同的内容放到另一个型号目录下
    _write_png(str(tmp_path / 'HL-002' / '主图.png'), 'red')

    report = run(app, str(tmp_path), embedding_client=CountingEmbedding())

    assert report.created == 1
    assert report.duplicates == 1
    assert ProductImage.query.count() == 1


def test_first_image_of_each_product_is_primary(app, tmp_path):
    from scripts.ingest_images import run

    _add_product('CS-001')
    _write_png(str(tmp_path / 'CS-001' / '1.png'), 'red')
    _write_png(str(tmp_path / 'CS-001' / '2.png'), 'blue')

    run(app, str(tmp_path), embedding_client=CountingEmbedding())

    primaries = ProductImage.query.filter_by(is_primary=True).all()
    assert len(primaries) == 1
    assert primaries[0].image_order == 0


def test_limit_caps_processed_images(app, tmp_path):
    from scripts.ingest_images import run

    _add_product('CS-001')
    for i, color in enumerate(('red', 'blue', 'green')):
        _write_png(str(tmp_path / 'CS-001' / f'{i}.png'), color)

    report = run(app, str(tmp_path), limit=2, embedding_client=CountingEmbedding())

    assert report.created == 2
    assert ProductImage.query.count() == 2


def test_batch_commit_integrity_error_does_not_abort_run_and_cleans_orphan_file(
    app, tmp_path, monkeypatch,
):
    """承重契约 (a)+(b)：某一批 commit 撞上 IntegrityError 时——

    (a) 只把这一批记为 failed，不能让整次导入中断，后续批次要继续跑；
    (b) 该批在 ingest_pending 阶段已经落盘的文件，rollback 后必须被删除，不留孤儿。

    用 monkeypatch 让第一次 db.session.commit() 抛 IntegrityError 来确定性地
    模拟"判重不加锁"文档里描述的并发撞车场景，而不依赖真实并发。
    """
    from sqlalchemy.exc import IntegrityError

    from scripts.ingest_images import run
    from services.ingest import hash_file, storage_paths

    _add_product('CS-001')
    _add_product('CS-002')
    first_path = str(tmp_path / 'CS-001' / '1.png')
    second_path = str(tmp_path / 'CS-002' / '1.png')
    _write_png(first_path, 'red')
    _write_png(second_path, 'blue')

    real_commit = db.session.commit
    calls = {'n': 0}

    def flaky_commit():
        calls['n'] += 1
        if calls['n'] == 1:
            raise IntegrityError('INSERT', {}, Exception('duplicate key value violates unique constraint'))
        return real_commit()

    monkeypatch.setattr(db.session, 'commit', flaky_commit)

    report = run(app, str(tmp_path), batch_size=1, embedding_client=CountingEmbedding())

    # (a) 第一批（CS-001，按 model_number 排序排第一）失败，第二批（CS-002）仍然成功
    assert report.failed == 1
    assert report.created == 1
    assert ProductImage.query.count() == 1
    assert ProductImage.query.first().model_number == 'CS-002'

    # (b) CS-001 那张图在 ingest_pending 阶段已经落盘，commit 失败 rollback 后不应遗留孤儿文件
    content_hash = hash_file(first_path)
    _, fs_path = storage_paths(app.config['UPLOAD_FOLDER'], 'CS-001', content_hash, '.png')
    assert not os.path.exists(fs_path)
