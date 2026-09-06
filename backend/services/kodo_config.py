"""七牛 Kodo S3 来源的环境配置。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

_S3_REGIONS = {
    "z0": "cn-east-1",
    "z1": "cn-north-1",
    "z2": "cn-south-1",
    "na0": "us-north-1",
    "as0": "ap-southeast-1",
    "cn-east-1": "cn-east-1",
    "cn-east-2": "cn-east-2",
    "cn-north-1": "cn-north-1",
    "cn-south-1": "cn-south-1",
    "us-north-1": "us-north-1",
    "ap-southeast-1": "ap-southeast-1",
    "ap-southeast-2": "ap-southeast-2",
    "ap-southeast-3": "ap-southeast-3",
}


class KodoConfigError(ValueError):
    """Kodo 环境变量缺失或不受支持。"""


@dataclass(frozen=True)
class KodoConfig:
    """只从环境变量构造的 Kodo S3 连接配置。"""

    access_key: str
    secret_key: str
    bucket_name: str
    qiniu_region: str
    s3_region: str
    endpoint_url: str
    aliases_used: tuple[str, ...] = ()
    s3_bucket_name: Optional[str] = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "KodoConfig":
        aliases_used: list[str] = []

        def read(name: str, *aliases: str) -> Optional[str]:
            value = environ.get(name)
            if value and value.strip():
                return value.strip()
            for alias in aliases:
                value = environ.get(alias)
                if value and value.strip():
                    aliases_used.append(alias)
                    return value.strip()
            return None

        access_key = read("QINIU_ACCESS_KEY", "AccessKey")
        secret_key = read("QINIU_SECRET_KEY", "SecretKey")
        bucket_name = read("QINIU_BUCKET_NAME", "BUCKET_NAME")
        qiniu_region = read("QINIU_REGION")

        missing = [
            name
            for name, value in (
                ("QINIU_ACCESS_KEY", access_key),
                ("QINIU_SECRET_KEY", secret_key),
                ("QINIU_BUCKET_NAME", bucket_name),
                ("QINIU_REGION", qiniu_region),
            )
            if not value
        ]
        if missing:
            raise KodoConfigError(f"缺少 Kodo 环境变量: {', '.join(missing)}")

        assert access_key is not None
        assert secret_key is not None
        assert bucket_name is not None
        assert qiniu_region is not None

        s3_region = _S3_REGIONS.get(qiniu_region)
        if not s3_region:
            raise KodoConfigError(
                "不支持的 QINIU_REGION；请使用已知地域简称或 S3 Region ID"
            )

        if aliases_used:
            logger.warning(
                "使用了兼容环境变量别名: %s",
                ", ".join(aliases_used),
            )

        return cls(
            access_key=access_key,
            secret_key=secret_key,
            bucket_name=bucket_name,
            qiniu_region=qiniu_region,
            s3_region=s3_region,
            endpoint_url=f"https://s3.{s3_region}.qiniucs.com",
            aliases_used=tuple(aliases_used),
            s3_bucket_name=read("QINIU_S3_BUCKET_NAME"),
        )
