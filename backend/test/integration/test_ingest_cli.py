"""旧本地 ProductImage CLI：只读盘点可用，所有写模式必须安全拒绝。"""

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
        return [
            np.full(1024, 0.1, dtype=np.float32)
            for _ in image_paths
        ]


def _add_product(model_number):
    db.session.add(Product(
        model_number=model_number,
        photographer_file='p',
        alibaba_product_url='https://example.com/x',
        category='相机肩带',
    ))
    db.session.commit()


def test_scan_directory_maps_dirname_to_model_number(tmp_path):
    from scripts.ingest_images import scan_directory

    _write_png(str(tmp_path / 'CS-001' / '1.png'), 'red')
    _write_png(str(tmp_path / 'CS-001' / '2.png'), 'blue')
    _write_png(str(tmp_path / 'HL-002' / '主图.PNG'), 'green')
    (tmp_path / 'CS-001' / 'notes.txt').write_text('忽略我')
    _write_png(str(tmp_path / '散图.png'), 'black')

    scanned = scan_directory(str(tmp_path))

    assert set(scanned) == {'CS-001', 'HL-002'}
    assert [
        os.path.basename(path) for path in scanned['CS-001']
    ] == ['1.png', '2.png']
    assert [
        os.path.basename(path) for path in scanned['HL-002']
    ] == ['主图.PNG']


def test_scan_directory_recurses_inside_model_directory(tmp_path):
    from scripts.ingest_images import scan_directory

    _write_png(str(tmp_path / 'CS-001' / '细节图' / 'a.png'), 'red')

    scanned = scan_directory(str(tmp_path))

    assert len(scanned['CS-001']) == 1
    assert scanned['CS-001'][0].endswith(
        os.path.join('细节图', 'a.png')
    )


def test_dry_run_reports_inventory_without_writes_or_embedding(app, tmp_path):
    from scripts.ingest_images import run

    _add_product('CS-001')
    _write_png(str(tmp_path / 'CS-001' / '1.png'), 'red')
    _write_png(str(tmp_path / 'UNKNOWN-001' / '1.png'), 'blue')
    embedding = CountingEmbedding()

    report = run(
        app,
        str(tmp_path),
        dry_run=True,
        embedding_client=embedding,
    )

    assert report.scanned == 2
    assert report.created == 1
    assert report.orphan_dirs == ['UNKNOWN-001']
    assert ProductImage.query.count() == 0
    assert embedding.image_calls == 0


def test_dry_run_reports_duplicate_content_without_old_table_write(
    app,
    tmp_path,
):
    from scripts.ingest_images import run

    _add_product('CS-001')
    _add_product('HL-002')
    _write_png(str(tmp_path / 'CS-001' / '1.png'), 'red')
    _write_png(str(tmp_path / 'HL-002' / '主图.png'), 'red')

    report = run(app, str(tmp_path), dry_run=True)

    assert report.created == 1
    assert report.duplicates == 1
    assert ProductImage.query.count() == 0


def test_dry_run_limit_caps_reported_pending_images(app, tmp_path):
    from scripts.ingest_images import run

    _add_product('CS-001')
    for index, color in enumerate(('red', 'blue', 'green')):
        _write_png(
            str(tmp_path / 'CS-001' / f'{index}.png'),
            color,
        )

    report = run(app, str(tmp_path), dry_run=True, limit=2)

    assert report.created == 2
    assert ProductImage.query.count() == 0


def test_write_mode_is_disabled_before_scan_embedding_or_database_write(
    app,
    tmp_path,
    monkeypatch,
):
    import scripts.ingest_images as ingest_images

    embedding = CountingEmbedding()

    def unexpected_scan(_root):
        raise AssertionError('禁用检查后不应继续扫描')

    monkeypatch.setattr(
        ingest_images,
        'scan_directory',
        unexpected_scan,
    )

    with pytest.raises(
        ingest_images.LegacyProductImageIngestDisabledError,
        match='ImageAssetIngestService',
    ):
        ingest_images.run(
            app,
            str(tmp_path),
            embedding_client=embedding,
        )

    assert embedding.image_calls == 0
    assert ProductImage.query.count() == 0


def test_rebuild_index_flag_cannot_bypass_disabled_write_mode(
    app,
    tmp_path,
):
    import scripts.ingest_images as ingest_images

    with pytest.raises(
        ingest_images.LegacyProductImageIngestDisabledError,
        match='已停用',
    ):
        ingest_images.run(
            app,
            str(tmp_path),
            rebuild_index=True,
        )

    assert ProductImage.query.count() == 0


def test_hash_file_is_called_once_per_image_in_dry_run(
    app,
    tmp_path,
    monkeypatch,
):
    import scripts.ingest_images as ingest_images

    _add_product('CS-001')
    for index, color in enumerate(('red', 'blue', 'green')):
        _write_png(
            str(tmp_path / 'CS-001' / f'{index}.png'),
            color,
        )
    real_hash_file = ingest_images.hash_file
    calls = {'count': 0}

    def counting_hash_file(path):
        calls['count'] += 1
        return real_hash_file(path)

    monkeypatch.setattr(
        ingest_images,
        'hash_file',
        counting_hash_file,
    )

    report = ingest_images.run(app, str(tmp_path), dry_run=True)

    assert report.created == 3
    assert calls['count'] == 3


def test_directories_without_images_are_reported_in_dry_run(app, tmp_path):
    from scripts.ingest_images import run

    _add_product('CS-001')
    root = tmp_path / 'source'
    os.makedirs(str(root / 'CS-001'))
    (root / 'CS-001' / 'notes.txt').write_text('无图片')
    os.makedirs(str(root / 'GHOST-000'))

    report = run(app, str(root), dry_run=True)

    assert sorted(report.empty_dirs) == ['CS-001', 'GHOST-000']
    assert report.orphan_dirs == []
    assert report.created == 0


def test_cli_help_points_to_the_image_asset_replacement():
    from scripts.ingest_images import create_parser

    parser = create_parser()

    assert '只读盘点' in parser.description
    assert 'migrate_kodo_to_oss' in (parser.epilog or '')
