"""持久图片导入的重试策略：错误分类、指数退避与尝试预算。

全部为纯函数与常量：不访问数据库、对象存储或模型服务，可被可控时间测试覆盖。
等待重试表达为持久 ``next_retry_at`` + 领取过滤；worker 绝不进程内 sleep 退避。
"""

from __future__ import annotations

import os

from services.embedding import (
    EmbeddingNetworkError,
    EmbeddingRateLimitExhaustedError,
    EmbeddingServerError,
    EmbeddingServiceError,
)
from services.object_storage import ObjectStorageError


MAX_AUTO_ATTEMPTS = 5
DEFAULT_RETRY_BASE_SECONDS = 30
DEFAULT_RETRY_CAP_SECONDS = 3600

ERROR_CLASS_RATE_LIMITED = 'rate_limited'
ERROR_CLASS_NETWORK = 'network'
ERROR_CLASS_SERVER_ERROR = 'server_error'
ERROR_CLASS_TRANSIENT_STORAGE = 'transient_storage'
ERROR_CLASS_STORAGE_MISSING = 'storage_missing'
ERROR_CLASS_EMBEDDING_INCOMPATIBLE = 'embedding_incompatible'
ERROR_CLASS_DETERMINISTIC_REQUEST = 'deterministic_request'
ERROR_CLASS_UNKNOWN = 'unknown'

RETRYABLE_ERROR_CLASSES = frozenset({
    ERROR_CLASS_RATE_LIMITED,
    ERROR_CLASS_NETWORK,
    ERROR_CLASS_SERVER_ERROR,
    ERROR_CLASS_TRANSIENT_STORAGE,
})

DETERMINISTIC_ERROR_CLASSES = frozenset({
    ERROR_CLASS_STORAGE_MISSING,
    ERROR_CLASS_EMBEDDING_INCOMPATIBLE,
    ERROR_CLASS_DETERMINISTIC_REQUEST,
    ERROR_CLASS_UNKNOWN,
})

ALL_ERROR_CLASSES = tuple(sorted(
    RETRYABLE_ERROR_CLASSES | DETERMINISTIC_ERROR_CLASSES
))


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def retry_base_seconds() -> int:
    return _positive_int_env(
        'IMAGE_IMPORT_RETRY_BASE_SECONDS', DEFAULT_RETRY_BASE_SECONDS
    )


def retry_cap_seconds() -> int:
    return _positive_int_env(
        'IMAGE_IMPORT_RETRY_CAP_SECONDS', DEFAULT_RETRY_CAP_SECONDS
    )


def next_retry_delay_seconds(attempt_count: int) -> int:
    """第 attempt_count 次尝试失败后到下次尝试的等待秒数（指数退避带上限）。"""
    if attempt_count < 1:
        raise ValueError('attempt_count 必须 >= 1')
    delay = retry_base_seconds() * (2 ** (attempt_count - 1))
    return min(delay, retry_cap_seconds())


def should_auto_retry(error_class: str, attempt_count: int) -> bool:
    """仅可重试分类且未耗尽自动尝试预算时返回 True。"""
    return (
        error_class in RETRYABLE_ERROR_CLASSES
        and attempt_count < MAX_AUTO_ATTEMPTS
    )


def classify_import_failure(exc: BaseException) -> str:
    """把 worker 处理链路上的异常映射为持久错误分类。

    只依据异常类型与结构化属性分类，绝不解析错误消息字符串。
    未知异常归为确定性 ``unknown``：不自动消耗重试预算，由手工重试兜底。
    """
    # 局部导入避免与 image_import_worker 的模块循环。
    from services.image_import_worker import InvalidEmbeddingResult

    if isinstance(exc, InvalidEmbeddingResult):
        return ERROR_CLASS_EMBEDDING_INCOMPATIBLE
    if isinstance(exc, EmbeddingRateLimitExhaustedError):
        return ERROR_CLASS_RATE_LIMITED
    if isinstance(exc, EmbeddingNetworkError):
        return ERROR_CLASS_NETWORK
    if isinstance(exc, EmbeddingServerError):
        return ERROR_CLASS_SERVER_ERROR
    if isinstance(exc, EmbeddingServiceError):
        return ERROR_CLASS_DETERMINISTIC_REQUEST
    if isinstance(exc, ObjectStorageError):
        if getattr(exc, 'stage', None) == 'download':
            if getattr(exc, 'status_code', None) == 404:
                return ERROR_CLASS_STORAGE_MISSING
            return ERROR_CLASS_TRANSIENT_STORAGE
        return ERROR_CLASS_UNKNOWN
    return ERROR_CLASS_UNKNOWN
