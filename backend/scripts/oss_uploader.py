#!/usr/bin/env python3
"""命令行工具：将本地文件或文件夹批量上传到阿里云OSS"""

import argparse
import os
import sys
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# 确保可以导入backend包
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT.parent))

from backend.blueprints.oss import (  # noqa: E402
    allowed_file,
    build_public_url,
    get_oss_client,
    sanitize_oss_prefix,
)


def gather_files(target: Path) -> List[Path]:
    if target.is_file():
        return [target]

    files: List[Path] = []
    for root, _, filenames in os.walk(target):
        root_path = Path(root)
        for filename in filenames:
            files.append(root_path / filename)
    return files


def upload_files(base_path: Path, files: List[Path], prefix: str, overwrite: bool) -> None:
    bucket, bucket_name = get_oss_client()
    endpoint = os.getenv('OSS_ENDPOINT', '')

    uploaded = 0
    skipped = 0

    for file_path in files:
        if not allowed_file(file_path.name):
            print(f"跳过不支持的文件: {file_path}")
            skipped += 1
            continue

        relative = file_path.name if base_path == file_path else file_path.relative_to(base_path)
        object_key = f"{prefix}/{relative.as_posix()}"

        if not overwrite and bucket.object_exists(object_key):
            print(f"跳过已存在的对象: {object_key}")
            skipped += 1
            continue

        with open(file_path, 'rb') as handler:
            bucket.put_object(object_key, handler)

        print(f"上传成功 -> {object_key}")
        print(f"访问URL: {build_public_url(bucket_name, endpoint, object_key)}\n")
        uploaded += 1

    print("上传完成")
    print(f"总计: {len(files)}, 成功: {uploaded}, 跳过: {skipped}")


def main() -> None:
    parser = argparse.ArgumentParser(description="上传文件或文件夹到阿里云OSS")
    parser.add_argument('path', help='待上传的文件或文件夹路径')
    parser.add_argument('--prefix', default='products', help='OSS目标前缀(默认: products)')
    parser.add_argument('--overwrite', action='store_true', help='若对象已存在则覆盖')

    args = parser.parse_args()

    load_dotenv()

    target_path = Path(args.path).expanduser().resolve()
    if not target_path.exists():
        raise SystemExit(f"路径不存在: {target_path}")

    prefix = sanitize_oss_prefix(args.prefix)

    if target_path.is_file():
        base_path = target_path.parent
    else:
        base_path = target_path

    files = gather_files(target_path)
    if not files:
        raise SystemExit("未找到可上传的文件")

    upload_files(base_path, files, prefix, args.overwrite)


if __name__ == '__main__':
    main()
