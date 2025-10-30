#!/usr/bin/env python3
"""
迁移脚本：将 product_images 表中的 original_path 转换为 OSS 路径并更新到 oss_path 字段
"""
import argparse
import logging
import re
import sys
from pathlib import Path

# 添加父目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import create_app
from models import db, ProductImage

# OSS 基础 URL
OSS_BASE_URL = "http://t4u5e1e4j.hd-bkt.clouddn.com"

# 需要提取的目录部分的正则匹配模式
# 例如: /Users/richardzhang/github/xiangyipackage/image-search-engine/backend/data/摄像师拍摄素材/2025.4.18海报照片/加拍照片/DSC01390.jpg
# 提取: 2025.4.18海报照片/加拍照片/DSC01390.jpg
DATASET_BASE_PATH = "/Users/richardzhang/github/xiangyipackage/image-search-engine/backend/data/摄像师拍摄素材"


def extract_relative_path(original_path: str) -> str:
    """
    从原始路径中提取相对路径部分

    Args:
        original_path: 完整的本地文件路径

    Returns:
        相对路径（从摄像师拍摄素材目录之后的部分）
    """
    if not original_path:
        return None

    # 使用 Path 对象处理路径
    path = Path(original_path)

    # 尝试提取相对于基础路径的部分
    try:
        # 如果路径包含基础路径，提取相对部分
        if DATASET_BASE_PATH in original_path:
            relative_part = original_path.split(DATASET_BASE_PATH + '/')[-1]
            return relative_part
        else:
            # 如果不包含基础路径，尝试从 "摄像师拍摄素材" 之后提取
            if "摄像师拍摄素材" in original_path:
                parts = original_path.split("摄像师拍摄素材/")
                if len(parts) > 1:
                    return parts[-1]

        # 如果以上方法都不适用，返回文件名的父目录+文件名（最后两级）
        if len(path.parts) >= 2:
            return f"{path.parts[-2]}/{path.name}"
        else:
            return path.name

    except Exception as e:
        logging.warning(f"提取相对路径失败: {original_path}, 错误: {e}")
        return path.name


def generate_oss_path(original_path: str) -> str:
    """
    根据 original_path 生成 OSS 路径

    Args:
        original_path: 原始本地文件路径

    Returns:
        OSS URL 路径
    """
    if not original_path:
        return None

    relative_path = extract_relative_path(original_path)
    if not relative_path:
        return None

    # 构建完整的 OSS URL
    oss_url = f"{OSS_BASE_URL}/{relative_path}"
    return oss_url


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="迁移 product_images 的 oss_path 字段")
    parser.add_argument(
        '--batch-size',
        type=int,
        default=100,
        help='数据库提交批次大小，默认 100'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='仅预览将执行的操作，不实际更新数据库'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='强制更新所有记录，包括已有 oss_path 的记录'
    )
    parser.add_argument(
        '--limit',
        type=int,
        help='仅处理指定数量的记录，用于测试'
    )
    return parser


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    parser = create_parser()
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        # 构建查询条件
        query = db.session.query(ProductImage).filter(
            ProductImage.original_path.isnot(None),
            ProductImage.original_path != ''
        )

        # 如果不是强制模式，只处理 oss_path 为空的记录
        if not args.force:
            query = query.filter(
                db.or_(
                    ProductImage.oss_path.is_(None),
                    ProductImage.oss_path == ''
                )
            )

        # 获取总数
        total_count = query.count()
        logging.info(f"找到 {total_count} 条需要处理的记录")

        if total_count == 0:
            logging.info("没有需要处理的记录")
            return

        # 应用限制
        if args.limit:
            query = query.limit(args.limit)
            logging.info(f"限制处理前 {args.limit} 条记录")

        # 获取所有需要处理的记录
        images = query.all()

        updated_count = 0
        error_count = 0
        skipped_count = 0

        for idx, image in enumerate(images, 1):
            try:
                original_path = image.original_path

                # 生成 OSS 路径
                oss_path = generate_oss_path(original_path)

                if not oss_path:
                    logging.warning(f"[{idx}/{len(images)}] 无法生成 OSS 路径: {original_path}")
                    skipped_count += 1
                    continue

                if args.dry_run:
                    logging.info(f"[{idx}/{len(images)}] [DRY-RUN] ID={image.id}")
                    logging.info(f"  原始路径: {original_path}")
                    logging.info(f"  OSS路径:  {oss_path}")
                else:
                    # 更新数据库
                    image.oss_path = oss_path
                    updated_count += 1

                    # 批量提交
                    if updated_count % args.batch_size == 0:
                        db.session.commit()
                        logging.info(f"[{idx}/{len(images)}] 已提交 {updated_count} 条更新")

            except Exception as e:
                db.session.rollback()
                error_count += 1
                logging.error(f"[{idx}/{len(images)}] 处理记录 ID={image.id} 时出错: {e}")
                logging.error(f"  原始路径: {image.original_path}")

        # 最后提交剩余的更新
        if not args.dry_run and updated_count % args.batch_size != 0:
            db.session.commit()
            logging.info(f"已提交最后一批更新")

        # 输出统计信息
        logging.info("=" * 60)
        logging.info("处理完成:")
        logging.info(f"  总记录数: {total_count}")
        logging.info(f"  处理数量: {len(images)}")
        logging.info(f"  更新成功: {updated_count}")
        logging.info(f"  跳过记录: {skipped_count}")
        logging.info(f"  错误记录: {error_count}")

        if args.dry_run:
            logging.info("\n[DRY-RUN 模式] 未实际更新数据库")
            logging.info("如需实际更新，请移除 --dry-run 参数")


if __name__ == '__main__':
    main()
