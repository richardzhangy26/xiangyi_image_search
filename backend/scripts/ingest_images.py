#!/usr/bin/env python3
"""旧本地目录图片入口（仅保留只读盘点）。

该脚本过去会把图片写入本机 ``uploads`` 和旧 ``product_images``。Issue #9
之后，正式图片必须经 ``ImageAssetIngestService`` 写入私有 OSS 与
``image_assets``，因此所有非 ``--dry-run`` 模式都会在扫描、embedding 或
数据库写入之前安全拒绝。

只读盘点：
    python -m scripts.ingest_images --root data/摄像师拍摄素材 --dry-run

Kodo 正式迁移：
    python -m scripts.migrate_kodo_to_oss --dry-run
"""

import argparse
import hashlib
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app import create_app  # noqa: E402
from models import ImageAsset, Product, db  # noqa: E402
from services.embedding import MAX_BATCH_SIZE  # noqa: E402

ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}


@dataclass
class PendingImage:
    """只读盘点阶段产出的候选项。"""

    model_number: str
    source_path: str
    content_hash: str
    image_order: int
    is_primary: bool


def hash_file(path):
    """流式计算文件 SHA-256，避免盘点大图时占用过多内存。"""

    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def find_existing_hashes(hashes):
    """读取 image_assets 中已有哈希，供只读盘点展示重复来源。"""

    if not hashes:
        return {}

    unique = list({value for value in hashes if value})
    found = {}
    for start in range(0, len(unique), 1000):
        chunk = unique[start:start + 1000]
        rows = db.session.query(
            ImageAsset.content_hash,
            ImageAsset.source_relative_path,
        ).filter(ImageAsset.content_hash.in_(chunk)).all()
        found.update({content_hash: source_path for content_hash, source_path in rows})
    return found

logger = logging.getLogger('ingest_images')

# 每张图约 402 tokens，0.0005 元/千 token；dry-run 仅用于估算。
YUAN_PER_IMAGE = 402 * 0.0005 / 1000

LEGACY_INGEST_DISABLED_MESSAGE = (
    'scripts.ingest_images 的 ProductImage/本地 uploads 写入已停用；'
    '正式图片必须通过 ImageAssetIngestService 写入私有 OSS 和 image_assets。'
    'Kodo 数据请改用 scripts.migrate_kodo_to_oss；本地目录目前仅支持 --dry-run '
    '只读盘点。'
)


class LegacyProductImageIngestDisabledError(RuntimeError):
    """旧 ProductImage 写入口被显式调用。"""


@dataclass
class IngestPlan:
    pending: list = field(default_factory=list)
    duplicates: list = field(default_factory=list)
    orphan_dirs: list = field(default_factory=list)


@dataclass
class IngestReport:
    created: int = 0
    duplicates: int = 0
    failed: int = 0
    orphan_dirs: list = field(default_factory=list)
    empty_dirs: list = field(default_factory=list)
    duplicate_details: list = field(default_factory=list)
    failed_details: list = field(default_factory=list)
    scanned: int = 0
    elapsed_seconds: float = 0.0


def scan_directory(root):
    """返回 ``{model_number: [排序后的图片绝对路径]}``。"""
    scanned = {}
    root_path = Path(root)
    for entry in sorted(root_path.iterdir()):
        if not entry.is_dir():
            continue
        images = sorted(
            str(path.resolve())
            for path in entry.rglob('*')
            if path.is_file()
            and path.suffix.lower() in ALLOWED_EXTENSIONS
        )
        if images:
            scanned[entry.name] = images
    return scanned


def _find_empty_directories(root, scanned):
    root_path = Path(root)
    return sorted(
        entry.name
        for entry in root_path.iterdir()
        if entry.is_dir() and entry.name not in scanned
    )


def build_plan(
    scanned,
    known_model_numbers,
    existing_hashes,
    path_hashes,
    limit=None,
):
    """构造只读盘点计划；不会生成向量、落盘或写数据库。"""
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

            content_hash = path_hashes[source_path]
            if content_hash in seen:
                plan.duplicates.append(
                    (source_path, seen[content_hash])
                )
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


def run(
    app,
    root,
    dry_run=False,
    rebuild_index=False,
    batch_size=MAX_BATCH_SIZE,
    limit=None,
    embedding_client=None,
):
    """执行只读盘点；任何旧表写模式均在触碰来源和外部服务前失败。"""
    if not dry_run:
        raise LegacyProductImageIngestDisabledError(
            LEGACY_INGEST_DISABLED_MESSAGE
        )

    # 保留旧调用签名，避免调用方误以为这些参数仍能开启写模式。
    del rebuild_index, batch_size, embedding_client

    started = time.perf_counter()
    report = IngestReport()
    try:
        with app.app_context():
            scanned = scan_directory(root)
            report.scanned = sum(
                len(paths) for paths in scanned.values()
            )
            report.empty_dirs = _find_empty_directories(root, scanned)
            known = {
                value
                for (value,) in db.session.query(
                    Product.model_number
                ).all()
            }
            path_hashes = {
                path: hash_file(path)
                for model_number, paths in scanned.items()
                if model_number in known
                for path in paths
            }
            existing = find_existing_hashes(
                list(path_hashes.values())
            )
            plan = build_plan(
                scanned,
                known,
                existing,
                path_hashes,
                limit=limit,
            )
            report.orphan_dirs = plan.orphan_dirs
            report.duplicates = len(plan.duplicates)
            report.duplicate_details = plan.duplicates
            report.created = len(plan.pending)
    except Exception:
        logger.exception('旧图片目录只读盘点失败')
        raise

    report.elapsed_seconds = time.perf_counter() - started
    return report


def print_report(report, dry_run):
    prefix = '[DRY-RUN] ' if dry_run else ''
    print(f'\n{prefix}扫描: {report.scanned} 张')
    print(f'  ✓ 将入库      {report.created} 张')
    print(
        '  ⊘ 重复跳过    '
        f'{report.duplicates} 张'
        f'（预计节省 ¥{report.duplicates * YUAN_PER_IMAGE:.3f}）'
    )
    for source, existing in report.duplicate_details[:20]:
        print(f'      {source}  与 {existing} 内容相同')
    print(
        f'  ✗ 孤儿目录    {len(report.orphan_dirs)} 个'
        '（无对应产品，已跳过）'
    )
    if report.orphan_dirs:
        print(f'      {", ".join(report.orphan_dirs)}')
    print(
        f'  ⊘ 无图片目录  {len(report.empty_dirs)} 个'
        '（目录存在但没有符合扩展名的图片）'
    )
    if report.empty_dirs:
        print(f'      {", ".join(report.empty_dirs)}')
    print(f'\n耗时 {report.elapsed_seconds:.1f}s；未写数据库、OSS 或本地 uploads。\n')


def create_parser():
    parser = argparse.ArgumentParser(
        description='旧本地图片目录只读盘点（ProductImage 写入已停用）。',
        epilog=(
            'Kodo 正式迁移请使用 python -m '
            'scripts.migrate_kodo_to_oss；本地文件夹写入 ImageAsset '
            '需等待独立入口。'
        ),
    )
    parser.add_argument(
        '--root',
        help='素材根目录，默认取 Flask 配置 DATASET_ROOT',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='只扫描算哈希并报告；这是当前唯一允许的模式',
    )
    parser.add_argument(
        '--rebuild-index',
        action='store_true',
        help='旧参数，仅用于给出停用错误，不再重建 product_images 索引',
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=MAX_BATCH_SIZE,
        help='旧参数；只读模式不会调用 embedding',
    )
    parser.add_argument(
        '--limit',
        type=int,
        help='只盘点前 N 张',
    )
    return parser


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='[%(levelname)s] %(message)s',
    )
    args = create_parser().parse_args()
    if not args.dry_run:
        raise SystemExit(LEGACY_INGEST_DISABLED_MESSAGE)

    app = create_app()
    root = args.root or app.config.get('DATASET_ROOT', '')
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        raise SystemExit(f'素材目录不存在: {root}')

    logger.info('素材目录: %s', root)
    report = run(
        app,
        root,
        dry_run=True,
        rebuild_index=args.rebuild_index,
        batch_size=args.batch_size,
        limit=args.limit,
    )
    print_report(report, dry_run=True)


if __name__ == '__main__':
    main()
