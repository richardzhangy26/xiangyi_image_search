"""Issue #20 重试策略与错误分类的纯单元测试（可控、无真实服务）。"""

from __future__ import annotations

import pytest

from services import import_retry
from services.embedding import (
    EmbeddingNetworkError,
    EmbeddingRateLimitExhaustedError,
    EmbeddingServerError,
    EmbeddingServiceError,
)
from services.image_import_worker import InvalidEmbeddingResult
from services.object_storage import ObjectStorageError


def test_delay_is_exponential_with_cap(monkeypatch):
    monkeypatch.delenv('IMAGE_IMPORT_RETRY_BASE_SECONDS', raising=False)
    monkeypatch.delenv('IMAGE_IMPORT_RETRY_CAP_SECONDS', raising=False)

    assert import_retry.next_retry_delay_seconds(1) == 30
    assert import_retry.next_retry_delay_seconds(2) == 60
    assert import_retry.next_retry_delay_seconds(3) == 120
    assert import_retry.next_retry_delay_seconds(4) == 240
    # 指数增长必须被上限截断
    assert import_retry.next_retry_delay_seconds(20) == 3600


def test_delay_respects_environment_overrides(monkeypatch):
    monkeypatch.setenv('IMAGE_IMPORT_RETRY_BASE_SECONDS', '10')
    monkeypatch.setenv('IMAGE_IMPORT_RETRY_CAP_SECONDS', '25')

    assert import_retry.next_retry_delay_seconds(1) == 10
    assert import_retry.next_retry_delay_seconds(2) == 20
    assert import_retry.next_retry_delay_seconds(3) == 25


def test_delay_rejects_non_positive_attempt_count():
    with pytest.raises(ValueError):
        import_retry.next_retry_delay_seconds(0)


def test_max_auto_attempts_is_five():
    assert import_retry.MAX_AUTO_ATTEMPTS == 5


@pytest.mark.parametrize(
    ('error_class', 'attempt_count', 'expected'),
    [
        ('rate_limited', 1, True),
        ('network', 4, True),
        ('server_error', 1, True),
        ('transient_storage', 2, True),
        ('rate_limited', 5, False),
        ('network', 6, False),
        ('storage_missing', 1, False),
        ('embedding_incompatible', 1, False),
        ('deterministic_request', 1, False),
        ('unknown', 1, False),
    ],
)
def test_should_auto_retry_only_retryable_classes_under_budget(
    error_class, attempt_count, expected
):
    assert import_retry.should_auto_retry(
        error_class, attempt_count
    ) is expected


def test_error_class_sets_are_disjoint_and_complete():
    assert import_retry.RETRYABLE_ERROR_CLASSES.isdisjoint(
        import_retry.DETERMINISTIC_ERROR_CLASSES
    )
    assert (
        import_retry.RETRYABLE_ERROR_CLASSES
        | import_retry.DETERMINISTIC_ERROR_CLASSES
    ) == set(import_retry.ALL_ERROR_CLASSES)


def test_classification_maps_transient_embedding_failures_to_retryable():
    assert import_retry.classify_import_failure(
        EmbeddingRateLimitExhaustedError('429重试3次后仍限流')
    ) == 'rate_limited'
    assert import_retry.classify_import_failure(
        EmbeddingNetworkError('图片向量提取失败: 连接超时')
    ) == 'network'
    assert import_retry.classify_import_failure(
        EmbeddingServerError('API调用失败(503): busy', status_code=503)
    ) == 'server_error'


def test_classification_maps_deterministic_embedding_failures():
    assert import_retry.classify_import_failure(
        InvalidEmbeddingResult('embedding 向量维度不匹配')
    ) == 'embedding_incompatible'
    assert import_retry.classify_import_failure(
        EmbeddingServiceError('标准化搜索预览图无法解码: UnidentifiedImageError')
    ) == 'deterministic_request'
    assert import_retry.classify_import_failure(
        EmbeddingServiceError('API调用失败(400): bad request')
    ) == 'deterministic_request'


def test_classification_maps_preview_download_failures():
    missing = ObjectStorageError('OSS 下载失败: NoSuchKey')
    missing.stage = 'download'
    missing.status_code = 404
    assert import_retry.classify_import_failure(missing) == 'storage_missing'

    transient = ObjectStorageError('OSS 下载失败: ConnectionError')
    transient.stage = 'download'
    assert import_retry.classify_import_failure(transient) == 'transient_storage'

    upload_scope = ObjectStorageError('OSS 上传失败')
    assert import_retry.classify_import_failure(upload_scope) == 'unknown'


def test_classification_never_parses_message_strings_for_unknown_types():
    assert import_retry.classify_import_failure(
        RuntimeError('some provider body text 429 503')
    ) == 'unknown'
