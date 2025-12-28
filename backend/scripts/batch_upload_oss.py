#!/usr/bin/env python3
"""
批量上传本地文件夹到阿里云 OSS，保留完整的目录结构。
默认读取 `backend/.env` 中的 OSS 配置。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from dotenv import load_dotenv

try:
    import oss2
except ImportError as exc:
    raise SystemExit("未找到 oss2 SDK，请先运行 'pip install oss2'。") from exc

# 常见图片/视频扩展名
DEFAULT_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tiff",
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".psd",  # 设计文件
}


def load_credentials(env_path: Path) -> None:
    """从指定 .env 文件加载阿里云 OSS 访问凭证。"""
    if not env_path.exists():
        raise SystemExit(f"找不到环境文件: {env_path}")
    load_dotenv(env_path)


def iter_local_files(root: Path, extensions: Optional[Iterable[str]]) -> Iterable[Path]:
    """递归遍历文件，返回所有匹配的文件路径。"""
    normalized_exts = None
    if extensions is not None:
        normalized_exts = {ext.lower() for ext in extensions}

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if normalized_exts is None:
            yield path
            continue
        if path.suffix.lower() in normalized_exts:
            yield path


def sanitize_oss_prefix(prefix: Optional[str], default: str = 'photographer') -> str:
    """清理 OSS 目标前缀，避免出现重复的 / 或 .. 路径。"""
    value = (prefix or default).strip()
    value = value.replace('\\', '/')
    parts = [segment for segment in value.split('/') if segment not in ('', '.', '..')]
    sanitized = '/'.join(parts)
    return sanitized or default


def build_remote_key(file_path: Path, root: Path, prefix: Optional[str]) -> str:
    """
    生成上传到 OSS 的对象 key，保持相对目录结构。

    例如:
    root = /Users/xxx/backend/data/摄像师拍摄素材
    file_path = /Users/xxx/backend/data/摄像师拍摄素材/手机挂绳/A49/DSC09614.jpg
    prefix = photographer

    返回: photographer/手机挂绳/A49/DSC09614.jpg
    """
    relative = file_path.relative_to(root).as_posix()
    if prefix:
        prefix = sanitize_oss_prefix(prefix)
        return f"{prefix}/{relative}"
    return relative


def build_public_url(bucket_name: str, endpoint: str, object_key: str) -> str:
    """根据 Bucket 和对象路径构建可访问 URL。"""
    endpoint = endpoint or ''
    if endpoint.startswith('http://'):
        endpoint = endpoint[7:]
    elif endpoint.startswith('https://'):
        endpoint = endpoint[8:]
    return f"https://{bucket_name}.{endpoint}/{object_key}"


def upload_files(
    root: Path,
    bucket: oss2.Bucket,
    bucket_name: str,
    endpoint: str,
    prefix: Optional[str],
    extensions: Optional[Iterable[str]],
    overwrite: bool,
    dry_run: bool,
) -> Dict[str, List[dict]]:
    """上传 root 下的文件，返回 {子目录: [{local_path, oss_key, url}]}。"""
    uploads: Dict[str, List[dict]] = {}
    skipped: Dict[str, List[dict]] = {}

    for file_path in iter_local_files(root, extensions):
        # 获取文件所属子目录（用于分组显示）
        folder_key = file_path.parent.relative_to(root).as_posix() or "."

        # 生成 OSS 对象键（保留完整路径）
        remote_key = build_remote_key(file_path, root, prefix)
        remote_url = build_public_url(bucket_name, endpoint, remote_key)

        if dry_run:
            print(f"[DRY-RUN] {file_path.relative_to(root)} -> {remote_key}")
            uploads.setdefault(folder_key, []).append({
                'local_path': str(file_path),
                'oss_key': remote_key,
                'url': remote_url
            })
            continue

        # 检查文件是否已存在
        if not overwrite and bucket.object_exists(remote_key):
            print(f"跳过已存在: {remote_key}")
            skipped.setdefault(folder_key, []).append({
                'local_path': str(file_path),
                'oss_key': remote_key,
                'reason': '已存在'
            })
            continue

        # 上传文件
        try:
            with open(file_path, 'rb') as local_file:
                bucket.put_object(remote_key, local_file)

            print(f"上传成功: {file_path.relative_to(root)} -> {remote_key}")
            uploads.setdefault(folder_key, []).append({
                'local_path': str(file_path),
                'oss_key': remote_key,
                'url': remote_url
            })
        except Exception as e:
            print(f"上传失败: {file_path} - {e}")
            skipped.setdefault(folder_key, []).append({
                'local_path': str(file_path),
                'oss_key': remote_key,
                'reason': str(e)
            })

    return {'uploaded': uploads, 'skipped': skipped}


def parse_extensions(value: Optional[str]) -> Optional[List[str]]:
    """解析命令行传入的扩展名列表。"""
    if not value:
        return None
    return [
        item.strip().lower() if item.strip().startswith(".") else f".{item.strip().lower()}"
        for item in value.split(",")
        if item.strip()
    ]


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="批量上传本地文件夹到阿里云 OSS，保留目录结构。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:

1. 上传摄像师拍摄素材到 OSS (默认行为):
   python batch_upload_oss.py

2. 上传到指定前缀 (OSS 路径):
   python batch_upload_oss.py --prefix photographer/raw-assets

3. 只上传 jpg 和 png 文件:
   python batch_upload_oss.py --extensions jpg,png

4. 预览上传计划（不实际上传）:
   python batch_upload_oss.py --dry-run

5. 覆盖已存在的文件:
   python batch_upload_oss.py --overwrite

6. 上传其他目录:
   python batch_upload_oss.py --root /path/to/your/folder

7. 保存上传结果到 JSON:
   python batch_upload_oss.py --output upload_result.json
        """
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "摄像师拍摄素材",
        help="待上传的根目录，默认使用 backend/data/摄像师拍摄素材",
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default="photographer",
        help="OSS 对象 key 的前缀（OSS 文件夹路径），例如 'photographer' 或 'photographer/raw-assets'。",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".env",
        help="包含 OSS 凭证的 .env 文件路径，默认 backend/.env",
    )
    parser.add_argument(
        "--extensions",
        type=str,
        help="用逗号分隔的扩展名列表，例如 'jpg,png,mp4'。留空表示上传全部文件",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖 OSS 中已存在的文件（默认跳过）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印将要上传的文件和目标路径，不执行上传",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="可选，将上传结果���入 JSON 文件",
    )
    return parser


def main() -> None:
    parser = create_parser()
    args = parser.parse_args()

    # 验证目录
    root = args.root.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"目录不存在或不是文件夹: {root}")

    # 加载环境变量
    load_credentials(args.env.expanduser().resolve())

    access_key_id = os.getenv("OSS_ACCESS_KEY_ID")
    access_key_secret = os.getenv("OSS_ACCESS_KEY_SECRET")
    endpoint = os.getenv("OSS_ENDPOINT")
    bucket_name = os.getenv("OSS_BUCKET_NAME")

    if not all([access_key_id, access_key_secret, endpoint, bucket_name]):
        raise SystemExit(
            "缺少 OSS 凭证，请确认 .env 中包含:\n"
            "  - OSS_ACCESS_KEY_ID\n"
            "  - OSS_ACCESS_KEY_SECRET\n"
            "  - OSS_ENDPOINT\n"
            "  - OSS_BUCKET_NAME"
        )

    # 解析扩展名
    extensions = parse_extensions(args.extensions) or (
        DEFAULT_EXTENSIONS if args.extensions is None else None
    )

    # 创建 OSS 客户端
    auth = oss2.Auth(access_key_id, access_key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)

    # 打印配置信息
    print(f"📁 本地目录: {root}")
    print(f"☁️  OSS Bucket: {bucket_name}")
    print(f"📂 OSS 前缀: {args.prefix or '(根目录)'}")
    print(f"🔧 文件类型: {', '.join(extensions) if extensions else '全部文件'}")
    print(f"{'🔁 覆盖模式' if args.overwrite else '⏭️  跳过已存在文件'}")
    if args.dry_run:
        print("⚠️  预览模式（不会实际上传）\n")
    else:
        print()

    # 执行上传
    results = upload_files(
        root=root,
        bucket=bucket,
        bucket_name=bucket_name,
        endpoint=endpoint,
        prefix=args.prefix,
        extensions=extensions,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )

    # 统计结果
    uploaded = results['uploaded']
    skipped = results['skipped']
    total_uploaded = sum(len(files) for files in uploaded.values())
    total_skipped = sum(len(files) for files in skipped.values())

    print(f"\n{'=' * 60}")
    print(f"✅ 上传成功: {total_uploaded} 个文件")
    print(f"⏭️  跳过文件: {total_skipped} 个文件")
    print(f"📊 总计: {total_uploaded + total_skipped} 个文件")
    print(f"{'=' * 60}\n")

    # 输出详细结果
    if args.output:
        output_path = args.output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        output_data = {
            'summary': {
                'total_uploaded': total_uploaded,
                'total_skipped': total_skipped,
                'root_directory': str(root),
                'oss_bucket': bucket_name,
                'oss_prefix': args.prefix,
            },
            'uploaded': uploaded,
            'skipped': skipped,
        }

        output_path.write_text(json.dumps(output_data, ensure_ascii=False, indent=2))
        print(f"📄 详细结果已写入: {output_path}")


if __name__ == "__main__":
    main()
