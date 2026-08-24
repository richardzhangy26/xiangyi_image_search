from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass


_UNCONFIGURED_DUMMY = hashlib.sha256(b"purge-admin-unconfigured").digest()


class AdminAuthError(Exception):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class AdminPrincipal:
    actor_id: str
    role: str = "admin"


def _sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _presented_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    parts = authorization_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    return parts[1]


class AdminAuth:
    def __init__(self, expected_token: str | None, actor_id: str = "admin"):
        stripped = (expected_token or "").strip()
        self._expected = stripped or None
        self._actor_id = actor_id

    def authenticate(self, authorization_header: str | None) -> AdminPrincipal:
        presented = _presented_token(authorization_header)
        presented_digest = _sha256(presented or "")
        if self._expected is None:
            hmac.compare_digest(presented_digest, _UNCONFIGURED_DUMMY)
            raise AdminAuthError(
                "AUTH_NOT_CONFIGURED",
                "永久清除控制面未配置管理员令牌",
            )
        matched = hmac.compare_digest(presented_digest, _sha256(self._expected))
        if presented is None:
            raise AdminAuthError("AUTH_REQUIRED", "需要管理员认证")
        if not matched:
            raise AdminAuthError(
                "AUTH_FORBIDDEN",
                "当前身份无权执行永久清除操作",
            )
        return AdminPrincipal(actor_id=self._actor_id, role="admin")
