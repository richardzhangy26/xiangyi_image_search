#!/usr/bin/env python3
"""
遍历指定目录下的子文件夹，批量将文件上传到七牛云 Kodo，并打印每个成功上传对象的访问路径。
默认读取 `backend/.env` 中的 AccessKey、SecretKey、BUCKET_NAME 配置。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from dotenv import load_dotenv

try:
    from qiniu import Auth, put_file
except ImportError as exc:  # pragma: no cover
    raise SystemExit("未找到 qiniu SDK，请先运行 'pip install qiniu'。") from exc

# 常见图片/视频扩展名，可通过命令行覆盖
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
}


def load_credentials(env_path: Path) -> None:
    """从指定 .env 文件加载七牛云访问凭证。"""
    if not env_path.exists():
        raise SystemExit(f"找不到环境文件: {env_path}")
    load_dotenv(env_path)


def iter_local_files(root: Path, extensions: Optional[Iterable[str]]) -> Iterable[Path]:
    """按目录结构遍历文件，返回所有匹配的文件路径。"""
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


def build_remote_key(file_path: Path, root: Path, prefix: Optional[str]) -> str:
    """生成上传到七牛的对象 key，保持相对目录结构。"""
    relative = file_path.relative_to(root).as_posix()
    if prefix:
        prefix = prefix.rstrip("/")
        return f"{prefix}/{relative}"
    return relative


def format_remote_url(key: str, domain: Optional[str]) -> str:
    """根据可选域名生成可访问的绝对路径。"""
    if domain:
        base = domain.rstrip("/")
        return f"{base}/{key}"
    # 未设置域名时返回七牛对象 key，便于后续拼接
    return key


def upload_files(
    root: Path,
    auth: Auth,
    bucket: str,
    domain: Optional[str],
    prefix: Optional[str],
    expires: int,
    extensions: Optional[Iterable[str]],
    dry_run: bool,
) -> Dict[str, List[str]]:
    """上传 root 下的文件，返回 {子目录: [远程路径]}。"""
    uploads: Dict[str, List[str]] = {}
    for file_path in iter_local_files(root, extensions):
        folder_key = file_path.parent.relative_to(root).as_posix() or "."
        remote_key = build_remote_key(file_path, root, prefix)
        remote_url = format_remote_url(remote_key, domain)

        if dry_run:
            print(f"[DRY-RUN] {file_path} -> {remote_url}")
            uploads.setdefault(folder_key, []).append(remote_url)
            continue

        token = auth.upload_token(bucket, remote_key, expires)
        ret, info = put_file(token, remote_key, str(file_path))
        if info.status_code != 200:
            print(f"上传失败: {file_path}")
            print(f"  状态码: {info.status_code}")
            if ret:
                print(f"  响应: {ret}")
            if getattr(info, "error", None):
                print(f"  错误: {info.error}")
            if getattr(info, "exception", None):
                print(f"  异常: {info.exception}")
            continue

        print(f"上传成功: {file_path} -> {remote_url}")
        uploads.setdefault(folder_key, []).append(remote_url)
    return uploads


def parse_extensions(value: Optional[str]) -> Optional[List[str]]:
    """解析命令行传入的扩展名列表。"""
    if not value:
        return None
    return [item.strip().lower() if item.strip().startswith(".") else f".{item.strip().lower()}" for item in value.split(",") if item.strip()]


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量上传本地文件夹到七牛云 Kodo。")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "摄像师拍摄素材",
        help="待上传的根目录，默认使用 backend/data/摄像师拍摄素材",
    )
    parser.add_argument(
        "--prefix",
        type=str,
        help="上传对象 key 的前缀，例如 'raw-assets'。",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".env",
        help="包含七牛云凭证的 .env 文件路径，默认 backend/.env",
    )
    parser.add_argument(
        "--expires",
        type=int,
        default=3600,
        help="上传凭证有效期（秒），默认 3600",
    )
    parser.add_argument(
        "--extensions",
        type=str,
        help="用逗号分隔的扩展名列表，例如 'jpg,png,mp4'。留空表示上传全部文件",
    )
    parser.add_argument(
        "--domain",
        type=str,
        help="覆盖 .env 中的 KODO_CDN_DOMAIN，显式指定访问域名",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印将要上传的文件和目标路径，不执行上传",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="可选，将上传结果写入 JSON 文件",
    )
    return parser


def main() -> None:
    parser = create_parser()
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"目录不存在或不是文件夹: {root}")

    load_credentials(args.env.expanduser().resolve())

    access_key = os.getenv("AccessKey")
    secret_key = os.getenv("SecretKey")
    bucket_name = os.getenv("BUCKET_NAME")
    if not access_key or not secret_key or not bucket_name:
        raise SystemExit("缺少七牛云凭证，请确认 .env 中包含 AccessKey、SecretKey、BUCKET_NAME。")

    domain = args.domain or os.getenv("KODO_CDN_DOMAIN")
    extensions = parse_extensions(args.extensions) or (DEFAULT_EXTENSIONS if args.extensions is None else None)

    auth = Auth(access_key, secret_key)
    uploads = upload_files(
        root=root,
        auth=auth,
        bucket=bucket_name,
        domain=domain,
        prefix=args.prefix,
        expires=args.expires,
        extensions=extensions,
        dry_run=args.dry_run,
    )

    print("\n上传结果:")
    print(json.dumps(uploads, ensure_ascii=False, indent=2))

    if args.output:
        output_path = args.output.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(uploads, ensure_ascii=False, indent=2))
        print(f"结果已写入: {output_path}")


if __name__ == "__main__":
    main()
