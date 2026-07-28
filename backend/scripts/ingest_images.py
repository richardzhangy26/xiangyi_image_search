#!/usr/bin/env python3
"""目录批量导入图片与向量。

约定：`--root` 的一级子目录名即 model_number，型号目录内部递归收图。
先跑 CSV 建产品，再跑本脚本导图；目录名对不上已有型号的一律跳过并报告。

用法：
    python -m scripts.ingest_images --root data/摄像师拍摄素材 --dry-run
    python -m scripts.ingest_images --root data/摄像师拍摄素材 --rebuild-index
"""
import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

from app import create_app  # noqa: E402
from models import Product, db  # noqa: E402
from services.embedding import MAX_BATCH_SIZE, EmbeddingClient  # noqa: E402
from services.ingest import (  # noqa: E402
    ALLOWED_EXTENSIONS,
    ImageIngestService,
    PendingImage,
    find_existing_hashes,
    hash_file,
)

logger = logging.getLogger('ingest_images')

# 每张图约 402 tokens，0.0005 元/千 token
YUAN_PER_IMAGE = 402 * 0.0005 / 1000

_HNSW_INDEX = 'idx_product_images_vector_hnsw'


@dataclass
class IngestPlan:
    pending: list = field(default_factory=list)          # list[PendingImage]
    duplicates: list = field(default_factory=list)       # list[(源路径, 已存在的 image_path)]
    orphan_dirs: list = field(default_factory=list)      # list[model_number]


@dataclass
class IngestReport:
    created: int = 0
    duplicates: int = 0
    failed: int = 0
    orphan_dirs: list = field(default_factory=list)
    duplicate_details: list = field(default_factory=list)
    failed_details: list = field(default_factory=list)
    scanned: int = 0
    elapsed_seconds: float = 0.0


def scan_directory(root):
    """{model_number: [排序后的图片绝对路径]}。

    一级子目录名 = model_number；型号目录内部递归收图。
    root 下直接存放的散图不属于任何型号，会被忽略（由调用方计入孤儿）。
    """
    scanned = {}
    root_path = Path(root)
    for entry in sorted(root_path.iterdir()):
        if not entry.is_dir():
            continue
        images = sorted(
            str(p.resolve()) for p in entry.rglob('*')
            if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS
        )
        if images:
            scanned[entry.name] = images
    return scanned


def build_plan(scanned, known_model_numbers, existing_hashes, limit=None):
    """把扫描结果切成「待入库 / 已重复 / 孤儿目录」三堆。

    existing_hashes: {content_hash: 已存在的 image_path}
    """
    plan = IngestPlan()
    seen = dict(existing_hashes)
    processed = 0

    for model_number in sorted(scanned):
        if model_number not in known_model_numbers:
            plan.orphan_dirs.append(model_number)
            continue

        for order, source_path in enumerate(scanned[model_number]):
            if limit is not None and processed >= limit:
                return plan
            processed += 1

            content_hash = hash_file(source_path)
            if content_hash in seen:
                plan.duplicates.append((source_path, seen[content_hash]))
                continue

            plan.pending.append(PendingImage(
                model_number=model_number,
                source_path=source_path,
                content_hash=content_hash,
                image_order=order,
                is_primary=(order == 0),
            ))
            seen[content_hash] = source_path

    return plan


def _cleanup_orphan_files(results):
    """批次 commit 失败 rollback 后，清理该批在 ingest_pending 阶段已经落盘的文件。

    承重契约 (b)：ImageIngestService 只 add 不 commit，成功项会立即落盘；
    调用方 rollback 后 DB 行消失但磁盘文件不会自动清理，必须用 IngestResult.fs_path
    自行删除，否则留下孤儿文件。这里只处理 status == 'created' 的项——duplicate/failed
    项本来就没有落盘（或落盘的是别的已提交的文件）。
    """
    for result in results or []:
        if result.status != 'created' or not result.fs_path:
            continue
        try:
            if os.path.exists(result.fs_path):
                os.remove(result.fs_path)
        except OSError as exc:
            logger.warning('清理孤儿文件失败 path=%s error=%s', result.fs_path, exc)


def _drop_hnsw_index():
    db.session.execute(text(f'DROP INDEX IF EXISTS {_HNSW_INDEX}'))
    db.session.commit()
    logger.info('已删除 HNSW 索引，导入结束后重建')


def _create_hnsw_index():
    # 建索引期间临时放宽内存与并行度（pgvector 官方建议）
    db.session.execute(text("SET maintenance_work_mem = '2GB'"))
    db.session.execute(text('SET max_parallel_maintenance_workers = 7'))
    db.session.execute(text(
        f'CREATE INDEX IF NOT EXISTS {_HNSW_INDEX} '
        'ON product_images USING hnsw (vector vector_cosine_ops) '
        'WITH (m = 16, ef_construction = 64)'
    ))
    db.session.execute(text('RESET maintenance_work_mem'))
    db.session.execute(text('RESET max_parallel_maintenance_workers'))
    db.session.execute(text('ANALYZE product_images'))
    db.session.commit()
    logger.info('HNSW 索引已重建')


def run(app, root, dry_run=False, rebuild_index=False, batch_size=MAX_BATCH_SIZE,
        limit=None, embedding_client=None):
    """执行导入，返回 IngestReport。app 需已进入 app_context 或本函数自行进入。"""
    started = time.perf_counter()

    def _execute():
        scanned = scan_directory(root)
        total_scanned = sum(len(v) for v in scanned.values())

        known = {
            value for (value,) in db.session.query(Product.model_number).all()
        }

        # 先批量算哈希，再一次性查库，避免逐张查询
        all_hashes = []
        for model_number, paths in scanned.items():
            if model_number in known:
                all_hashes.extend(hash_file(p) for p in paths)
        existing = find_existing_hashes(all_hashes)

        plan = build_plan(scanned, known, existing, limit=limit)

        report = IngestReport(
            duplicates=len(plan.duplicates),
            orphan_dirs=plan.orphan_dirs,
            duplicate_details=plan.duplicates,
            scanned=total_scanned,
        )

        if dry_run:
            report.created = len(plan.pending)
            return report

        service = ImageIngestService(embedding_client=embedding_client or EmbeddingClient())
        upload_folder = app.config['UPLOAD_FOLDER']
        effective_batch = max(1, min(int(batch_size), MAX_BATCH_SIZE))

        for start in range(0, len(plan.pending), effective_batch):
            chunk = plan.pending[start:start + effective_batch]
            results = None
            try:
                results = service.ingest_pending(chunk, upload_folder)
                db.session.commit()
            except Exception as exc:  # noqa: BLE001 - 单批失败不应终止整次导入
                db.session.rollback()
                logger.error('批次写入失败 start=%s size=%s error=%s', start, len(chunk), exc)
                # 判重不加锁，最终唯一性靠 DB UNIQUE 约束兜底：commit 撞上
                # IntegrityError 时，本批在 ingest_pending 阶段已经落盘的文件
                # 不会随 rollback 自动消失，必须显式清理，否则留下孤儿文件。
                _cleanup_orphan_files(results)
                report.failed += len(chunk)
                report.failed_details.extend((item.source_path, str(exc)) for item in chunk)
                continue

            for result in results:
                if result.status == 'created':
                    report.created += 1
                elif result.status == 'duplicate':
                    report.duplicates += 1
                    report.duplicate_details.append((result.source_path, result.duplicate_of))
                else:
                    report.failed += 1
                    report.failed_details.append((result.source_path, result.error))

            logger.info('进度 %s/%s', min(start + effective_batch, len(plan.pending)),
                        len(plan.pending))

        return report

    if rebuild_index and not dry_run:
        with app.app_context():
            _drop_hnsw_index()

    # 测试传入的 app fixture 已处于 app_context 内；Flask 支持嵌套，无冲突
    with app.app_context():
        report = _execute()

    if rebuild_index and not dry_run:
        with app.app_context():
            _create_hnsw_index()

    report.elapsed_seconds = time.perf_counter() - started
    return report


def print_report(report, dry_run):
    prefix = '[DRY-RUN] ' if dry_run else ''
    print(f'\n{prefix}扫描: {report.scanned} 张')
    print(f'  ✓ {"将入库" if dry_run else "入库"}      {report.created} 张')
    print(f'  ⊘ 重复跳过    {report.duplicates} 张（节省 ¥{report.duplicates * YUAN_PER_IMAGE:.3f}）')
    for source, existing in report.duplicate_details[:20]:
        print(f'      {source}  与 {existing} 内容相同')
    if len(report.duplicate_details) > 20:
        print(f'      …… 其余 {len(report.duplicate_details) - 20} 条见日志')
    print(f'  ✗ 孤儿目录    {len(report.orphan_dirs)} 个（无对应产品，已跳过）')
    if report.orphan_dirs:
        print(f'      {", ".join(report.orphan_dirs)}')
    print(f'  ✗ 失败        {report.failed} 张')
    for source, error in report.failed_details[:20]:
        print(f'      {source}: {error}')
    print(f'\n耗时 {report.elapsed_seconds:.1f}s / API 费用约 ¥{report.created * YUAN_PER_IMAGE:.2f}\n')


def create_parser():
    parser = argparse.ArgumentParser(description='批量导入本地目录中的产品图片与向量。')
    parser.add_argument('--root', help='素材根目录，默认取 Flask 配置 DATASET_ROOT')
    parser.add_argument('--dry-run', action='store_true',
                        help='只扫描算哈希查重并报告，不调 API、不写库、不落盘')
    parser.add_argument('--rebuild-index', action='store_true',
                        help='导入前删除 HNSW 索引，导入后重建（首次全量导入用）')
    parser.add_argument('--batch-size', type=int, default=MAX_BATCH_SIZE,
                        help=f'每次 DashScope 请求的图片数，clamp 到 [1, {MAX_BATCH_SIZE}]')
    parser.add_argument('--limit', type=int, help='只处理前 N 张，用于调试')
    return parser


def main():
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
    args = create_parser().parse_args()

    app = create_app()
    root = args.root or app.config.get('DATASET_ROOT', '')
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        raise SystemExit(f'素材目录不存在: {root}')

    logger.info('素材目录: %s', root)
    report = run(
        app, root,
        dry_run=args.dry_run,
        rebuild_index=args.rebuild_index,
        batch_size=args.batch_size,
        limit=args.limit,
    )
    print_report(report, args.dry_run)


if __name__ == '__main__':
    main()
