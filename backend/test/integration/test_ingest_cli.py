"""目录批量导入 CLI：孤儿目录报告、dry-run、幂等。"""
import io
import os

import numpy as np
import pytest
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


def test_cleanup_does_not_delete_file_still_referenced_by_a_committed_row(
    app, tmp_path, monkeypatch,
):
    """[Fix round 1 · Critical] 跨批次场景：第一次写入成功提交；第二次因为
    「判重不加锁」（比如并发下 find_existing_hashes 查询时还没看到第一次的
    提交）又把同一内容（同一 content_hash → 同一路径，文件名由哈希决定）当成
    新图送进 ingest_pending，commit 时撞真实的 content_hash UNIQUE 约束。

    rollback 后的清理逻辑绝不能把第一次已提交的合法文件删掉——否则数据库行
    还在、文件却没了，图片直接 404，而且不会出现在任何"孤儿文件"报告里，
    比 brief 想避免的普通孤儿文件更隐蔽也更危险。
    """
    import scripts.ingest_images as ingest_images_module
    from services.ingest import hash_file, storage_paths

    _add_product('CS-001')
    image_path = str(tmp_path / 'CS-001' / '1.png')
    _write_png(image_path, 'red')

    # 第一次：正常导入，成功落盘并提交
    first = ingest_images_module.run(app, str(tmp_path), embedding_client=CountingEmbedding())
    assert first.created == 1
    assert ProductImage.query.count() == 1

    content_hash = hash_file(image_path)
    _, fs_path = storage_paths(app.config['UPLOAD_FOLDER'], 'CS-001', content_hash, '.png')
    assert os.path.exists(fs_path)

    # 第二次：模拟"判重不加锁"的并发场景——find_existing_hashes 查不到刚提交的
    # 那一行（比如另一个写入者在自己做判重查询之后、commit 之前，第一个写入者
    # 才提交成功），导致同一张图又被当成"新图"送进 ingest_pending。
    monkeypatch.setattr(ingest_images_module, 'find_existing_hashes', lambda hashes: {})

    second = ingest_images_module.run(app, str(tmp_path), embedding_client=CountingEmbedding())

    # commit 时撞真实的 UNIQUE 约束，本批记为 failed
    assert second.failed == 1
    assert ProductImage.query.count() == 1  # 没有变成孤儿数据库行——还是只有第一次那一条

    # 关键断言：第一次已提交的文件不能被第二次的 rollback-cleanup 误删
    assert os.path.exists(fs_path)


def test_hash_file_is_called_once_per_image_not_twice(app, tmp_path, monkeypatch):
    """[Fix round 1 · Important 1] 每张已知型号的图片只应该被 hash_file 计算
    一次：find_existing_hashes 批量查库用的哈希、build_plan 判重用的哈希，
    必须是同一份，不能重复计算——几千张图的目录下，重复计算是实打实的
    I/O + CPU 翻倍。
    """
    import scripts.ingest_images as ingest_images_module

    _add_product('CS-001')
    for i, color in enumerate(('red', 'blue', 'green')):
        _write_png(str(tmp_path / 'CS-001' / f'{i}.png'), color)

    real_hash_file = ingest_images_module.hash_file
    calls = {'n': 0}

    def counting_hash_file(path):
        calls['n'] += 1
        return real_hash_file(path)

    monkeypatch.setattr(ingest_images_module, 'hash_file', counting_hash_file)

    report = ingest_images_module.run(app, str(tmp_path), embedding_client=CountingEmbedding())

    assert report.created == 3
    assert calls['n'] == 3  # 3 张图，每张只算一次哈希（而不是 6 次）


def test_rebuild_index_survives_unexpected_exception_during_execute(
    app, tmp_path, monkeypatch,
):
    """[Fix round 1 · Important 2] --rebuild-index 用 try/finally 兜底：
    DROP 之后哪怕 _execute() 内部抛出未被批次级 try/except 吞掉的异常
    （比如扫描阶段本身出错），索引也必须被重建，不能永久丢失。
    """
    from sqlalchemy import text

    import scripts.ingest_images as ingest_images_module

    _add_product('CS-001')
    _write_png(str(tmp_path / 'CS-001' / '1.png'), 'red')

    def _boom(root):
        raise RuntimeError('模拟扫描阶段的未预期异常')

    monkeypatch.setattr(ingest_images_module, 'scan_directory', _boom)

    with pytest.raises(RuntimeError, match='模拟扫描阶段的未预期异常'):
        ingest_images_module.run(
            app, str(tmp_path), rebuild_index=True, embedding_client=CountingEmbedding(),
        )

    exists = db.session.execute(text(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_product_images_vector_hnsw'"
    )).scalar()
    assert exists == 1


def test_directories_without_images_are_reported_as_empty(app, tmp_path):
    """[Fix round 1 · Minor] 目录存在但递归后没有任何符合扩展名的图片（真空
    目录，或只有 .txt 之类的非图片文件），既不该进 scanned，也不该被静默
    忽略——哪怕目录名对不上任何已知型号，也要在报告里给出信号。

    素材根目录用 tmp_path 的子目录 'source'，与 app fixture 的
    UPLOAD_FOLDER（tmp_path/'uploads'）区分开——否则 UPLOAD_FOLDER 刚创建时
    也是空目录，会被一起扫描进来，污染 empty_dirs 的断言（生产环境里 --root
    素材目录和 UPLOAD_FOLDER 从来不是同一个目录树，这里只是测试隔离需要）。
    """
    from scripts.ingest_images import run

    _add_product('CS-001')
    root = tmp_path / 'source'
    os.makedirs(str(root / 'CS-001'))  # 型号目录存在，但没有图片
    (root / 'CS-001' / 'notes.txt').write_text('无图片')
    os.makedirs(str(root / 'GHOST-000'))  # 目录名对不上任何型号，也没有图片

    report = run(app, str(root), embedding_client=CountingEmbedding())

    assert sorted(report.empty_dirs) == ['CS-001', 'GHOST-000']
    assert report.orphan_dirs == []  # 没图片的目录不会同时出现在"孤儿目录"里，避免重复报告
    assert report.created == 0
