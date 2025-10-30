#!/usr/bin/env python3
"""
批量导入本地数据集图片并构建向量索引。
"""
import argparse
import json
import logging
from pathlib import Path
from typing import Iterable, Set

from sqlalchemy import func

from app import create_app
from models import db, Product, ProductImage
from product_search import VectorProductIndex

ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}


def iter_image_files(root: Path) -> Iterable[Path]:
    """递归遍历目录，返回所有符合扩展名的图片文件。"""
    for path in sorted(root.rglob('*')):
        if path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS:
            yield path


def load_existing_paths() -> Set[str]:
    """获取已处理图片的原始路径集合，用于跳过重复导入。"""
    rows = (
        db.session.query(ProductImage.original_path)
        .filter(ProductImage.original_path.isnot(None))
        .all()
    )
    return {row[0] for row in rows if row[0]}


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量导入图片到商品库并更新向量索引。")
    parser.add_argument(
        '--root',
        type=Path,
        help='图片数据集根目录，默认读取 Flask 配置 DATASET_ROOT。'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=10,
        help='数据库提交批次大小，默认 10。'
    )
    parser.add_argument(
        '--limit',
        type=int,
        help='仅处理指定数量的图片，用于调试。'
    )
    parser.add_argument(
        '--start-id',
        type=int,
        help='自定义起始产品ID，默认基于当前最大ID顺延。'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='仅打印将执行的操作，不写入数据库或调用向量服务。'
    )
    parser.add_argument(
        '--reprocess',
        action='store_true',
        help='重新处理已存在于 product_images.original_path 的图片。'
    )
    return parser


def main():
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
    parser = create_parser()
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        dataset_root = Path(args.root or app.config.get('DATASET_ROOT', '')).expanduser().resolve()
        if not dataset_root.exists():
            raise SystemExit(f'数据集目录不存在: {dataset_root}')
        logging.info('使用数据集目录: %s', dataset_root)

        product_index = app.config.get('PRODUCT_INDEX')
        if product_index is None:
            product_index = VectorProductIndex()
            app.config['PRODUCT_INDEX'] = product_index

        existing_paths = set()
        if not args.reprocess:
            existing_paths = load_existing_paths()
            logging.info('已加载 %d 条已处理图片路径，将跳过重复项。', len(existing_paths))


        processed = 0
        created = 0
        skipped = 0
        errors = 0

        for file_path in iter_image_files(dataset_root):
            if args.limit and processed >= args.limit:
                break

            processed += 1
            original_path = str(file_path.resolve())
            if not args.reprocess and original_path in existing_paths:
                skipped += 1
                continue

            relative_path = file_path.relative_to(dataset_root).as_posix()
            web_path = f"/dataset-images/{relative_path}"

            if args.dry_run:
                logging.info('[DRY-RUN] 将导入图片: %s -> 产品ID %d', original_path)
                existing_paths.add(original_path)
                created += 1
                continue

            try:
                product_name = relative_path[-200:]
                product_description = f'自动导入: {relative_path}'

                product = Product(
                    name=product_name,
                    description=product_description,
                    price=0.0,
                    sale_price=None,
                    product_code=None,
                    image_url=web_path,
                    image_path=web_path,
                    good_img=json.dumps(
                        [{'url': web_path, 'tag': None, 'original_path': original_path}],
                        ensure_ascii=False
                    )
                )
                db.session.add(product)
                db.session.flush()  # 生成 product.id

                feature = product_index.extract_feature(str(file_path))
                product_image = ProductImage(
                    product_id=product.id,
                    image_path=web_path,
                    original_path=original_path,
                    vector=feature.tobytes()
                )
                db.session.add(product_image)

                existing_paths.add(original_path)
                created += 1

                if created % args.batch_size == 0:
                    db.session.commit()
                    logging.info('已提交 %d 条新图片。', created)

            except Exception as exc:
                db.session.rollback()
                errors += 1
                logging.exception('处理图片 "%s" 时出错: %s', original_path, exc)

        if not args.dry_run:
            db.session.commit()
            product_index.refresh_from_database()
            index_path = app.config['INDEX_PATH']
            product_index.save_index(index_path)
            logging.info('向量索引已刷新并保存到 %s', index_path)

        logging.info('处理完成: 新增 %d, 跳过 %d, 错误 %d, 总计遍历 %d', created, skipped, errors, processed)


if __name__ == '__main__':
    main()
