#!/usr/bin/env python3
"""
使用七牛云 Kodo SDK 上传本地图片，便于快速验证凭证和桶配置是否正确。
"""
import argparse
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

try:
    from qiniu import Auth, put_file
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "未找到 qiniu SDK，请先运行 'pip install qiniu' 后再重试。"
    ) from exc


def load_credentials(env_path: Path) -> None:
    """加载 .env 文件中的七牛云凭证。"""
    if not env_path.exists():
        raise SystemExit(f"找不到环境文件: {env_path}")
    load_dotenv(env_path)


def resolve_key(file_path: Path, key: Optional[str], prefix: Optional[str]) -> str:
    """根据参数生成上传到七牛的对象 key。"""
    candidate = key or file_path.name
    if prefix:
        prefix = prefix.rstrip('/')
        return f"{prefix}/{candidate}" if candidate else prefix
    return candidate


def main() -> None:
    parser = argparse.ArgumentParser(description="上传本地图片到七牛云 Kodo 以便测试配置。")
    parser.add_argument("file", type=Path, help="待上传的本地图片路径")
    parser.add_argument(
        "--key",
        type=str,
        help="上传时使用的对象 key，默认使用文件名",
    )
    parser.add_argument(
        "--prefix",
        type=str,
        help="可选的 key 前缀，例如 'test-uploads'",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".env",
        help="凭证所在的 .env 文件路径，默认使用 backend/.env",
    )
    parser.add_argument(
        "--expires",
        type=int,
        default=3600,
        help="上传凭证有效期（秒），默认 3600",
    )
    args = parser.parse_args()

    load_credentials(args.env)

    access_key = os.getenv("AccessKey")
    secret_key = os.getenv("SecretKey")
    bucket_name = os.getenv("BUCKET_NAME")

    if not access_key or not secret_key or not bucket_name:
        raise SystemExit("缺少七牛云凭证，请确认 .env 文件包含 AccessKey、SecretKey 和 BUCKET_NAME。")

    file_path = args.file.expanduser().resolve()
    if not file_path.exists():
        raise SystemExit(f"文件不存在: {file_path}")
    if not file_path.is_file():
        raise SystemExit(f"路径不是文件: {file_path}")

    upload_key = resolve_key(file_path, args.key, args.prefix)

    auth = Auth(access_key, secret_key)
    token = auth.upload_token(bucket_name, upload_key, args.expires)

    print(f"开始上传 {file_path} -> {bucket_name}:{upload_key}")
    ret, info = put_file(token, upload_key, str(file_path))

    if info.status_code == 200:
        payload = ret or {}
        etag = payload.get("key") or payload.get("hash")
        print("上传成功！")
        print(f"响应: {payload}")
        if domain := os.getenv("KODO_CDN_DOMAIN"):
            domain = domain.rstrip('/')
            print(f"访问链接: {domain}/{upload_key}")
        else:
            print("如需直接访问链接，可在 .env 中添加 KODO_CDN_DOMAIN 配置。")
        if etag:
            print(f"ETag: {etag}")
    else:
        print("上传失败。")
        print(f"状态码: {info.status_code}")
        if ret:
            print(f"响应: {ret}")
        if getattr(info, "error", None):
            print(f"错误: {info.error}")
        if getattr(info, "exception", None):
            print(f"异常: {info.exception}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
