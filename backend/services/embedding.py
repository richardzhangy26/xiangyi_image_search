"""DashScope 多模态向量客户端。

职责边界：图片读取/压缩/base64、单张与批量 embedding 调用、429 重试、批失败降级。
不涉及数据库，不涉及去重。
"""
import base64
import io
import logging
import os
import time
from http import HTTPStatus

import dashscope
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# 多模态向量模型（Qwen3 底座，图文同空间，支持文搜图）
EMBEDDING_MODEL = 'tongyi-embedding-vision-plus-2026-03-06'
EMBEDDING_DIMENSION = 1024

# DashScope 实测硬上限：一次请求内容元素数 > 20 会返回
# 400 "contents count (N) exceeds limit (20)"
MAX_BATCH_SIZE = 20

MAX_IMAGE_MB = 2.5


class EmbeddingServiceError(Exception):
    """图片向量提取服务异常。"""


class EmbeddingRateLimitExhaustedError(EmbeddingServiceError):
    """429 限流重试已耗尽。

    与其他失败（如坏图导致的 400）不同：这种情况下逐张重试没有意义——
    账号级限流不会因为把一批拆成多次单张调用就消失，反而会发起更多请求、
    加剧限流。调用方（_embed_chunk）据此区分是否要降级。
    """


def _to_data_uri(image_path, max_size_mb=MAX_IMAGE_MB):
    """读图 → 必要时压缩到 max_size_mb 以内 → JPEG base64 Data URI。"""
    image = Image.open(image_path)
    if image.mode not in ('RGB', 'L'):
        image = image.convert('RGB')

    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=95)
    img_bytes = buffer.getvalue()

    max_size_bytes = int(max_size_mb * 1024 * 1024)
    if len(img_bytes) > max_size_bytes:
        width, height = image.size
        scale = (max_size_bytes / len(img_bytes)) ** 0.5 * 0.9  # 0.9 安全系数
        image = image.resize((int(width * scale), int(height * scale)), Image.Resampling.LANCZOS)

        quality = 85
        while quality > 50:
            buffer = io.BytesIO()
            image.save(buffer, format='JPEG', quality=quality)
            img_bytes = buffer.getvalue()
            if len(img_bytes) <= max_size_bytes:
                break
            quality -= 5
        logger.info(
            'embedding.compress image_path=%s final_mb=%.2f quality=%s',
            image_path, len(img_bytes) / 1024 / 1024, quality,
        )

    return 'data:image/jpeg;base64,' + base64.b64encode(img_bytes).decode('utf-8')


def _normalized_to_data_uri(image_path, max_size_mb=MAX_IMAGE_MB):
    """将 ImageNormalizer 产出的合规 JPEG 原样转换为 Data URI。"""
    max_size_bytes = int(max_size_mb * 1024 * 1024)
    if os.path.getsize(image_path) > max_size_bytes:
        raise EmbeddingServiceError('标准化搜索预览图超过 embedding 大小上限')

    with open(image_path, 'rb') as source:
        preview_bytes = source.read()
    try:
        with Image.open(io.BytesIO(preview_bytes)) as preview:
            if preview.format != 'JPEG':
                raise EmbeddingServiceError('标准化搜索预览图必须是 JPEG')
            preview.verify()
    except EmbeddingServiceError:
        raise
    except Exception as exc:
        raise EmbeddingServiceError(
            f'标准化搜索预览图无法解码: {type(exc).__name__}'
        ) from exc

    return (
        'data:image/jpeg;base64,'
        + base64.b64encode(preview_bytes).decode('utf-8')
    )


class EmbeddingClient:
    """无状态的 DashScope 封装，可安全地被多个请求共享。"""

    def __init__(self, api_key=None, max_retries=3, initial_delay=5.0):
        self.api_key = api_key or os.getenv('DASHSCOPE_API_KEY')
        if not self.api_key:
            logger.warning('DASHSCOPE_API_KEY 未设置，embedding 调用将失败')
        self.max_retries = max_retries
        self.initial_delay = initial_delay

    # ---------- 对外接口 ----------

    def embed_image(self, image_path, request_id=None):
        """单张图片 → 1024 维向量。失败抛 EmbeddingServiceError。"""
        return self._call([{'image': _to_data_uri(image_path)}], request_id)[0]

    def embed_normalized_image(self, image_path, request_id=None):
        """标准化搜索预览 JPEG → 1024 维向量，不做二次有损转码。"""
        return self._call(
            [{'image': _normalized_to_data_uri(image_path)}],
            request_id,
        )[0]

    def embed_text(self, content, request_id=None):
        """文本 → 1024 维向量。与图片共享同一向量空间。"""
        return self._call([{'text': content}], request_id)[0]

    def embed_images(self, image_paths, request_id=None):
        """批量图片 → 向量列表，长度与入参一致，失败项为 None。

        一批中只要有一张坏图，整个请求就会 400。因此批级失败时降级为逐张调用，
        只把真正有问题的图片标记为 None，避免一张坏图毁掉 20 张。
        """
        if not image_paths:
            return []

        results = []
        for start in range(0, len(image_paths), MAX_BATCH_SIZE):
            chunk = image_paths[start:start + MAX_BATCH_SIZE]
            results.extend(self._embed_chunk(chunk, request_id))
        return results

    # ---------- 内部实现 ----------

    def _embed_chunk(self, chunk, request_id):
        try:
            inputs = [{'image': _to_data_uri(path)} for path in chunk]
            return self._call(inputs, request_id)
        except EmbeddingRateLimitExhaustedError as exc:
            # 429 重试已耗尽：逐张重试同样会被限流，只会发起更多请求、加剧限流。
            # 不降级，整批直接判定失败（保持 embed_images 的长度契约：全部记为 None）。
            logger.error(
                'embedding.batch.rate_limited request_id=%s size=%s error=%s',
                request_id, len(chunk), exc,
            )
            return [None] * len(chunk)
        except Exception as exc:  # noqa: BLE001 - 非限流原因（如坏图 400），降级逐张定位
            logger.warning(
                'embedding.batch.degraded request_id=%s size=%s error=%s',
                request_id, len(chunk), exc,
            )

        degraded = []
        for path in chunk:
            try:
                degraded.append(self.embed_image(path, request_id=request_id))
            except Exception as exc:  # noqa: BLE001 - 单张失败只影响该张
                logger.error(
                    'embedding.single.failed request_id=%s image_path=%s error=%s',
                    request_id, path, exc,
                )
                degraded.append(None)
        return degraded

    def _call(self, inputs, request_id):
        """带 429 指数退避的 DashScope 调用，返回 np.ndarray 列表（按 index 排序）。"""
        if self.max_retries <= 0:
            raise EmbeddingServiceError('EmbeddingClient.max_retries 必须 >= 1')

        start = time.perf_counter()
        delay = self.initial_delay

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = dashscope.MultiModalEmbedding.call(
                    model=EMBEDDING_MODEL,
                    input=inputs,
                    dimension=EMBEDDING_DIMENSION,
                    api_key=self.api_key,
                )
            except Exception as exc:  # SDK 层异常（网络等），不重试
                raise EmbeddingServiceError(f'图片向量提取失败: {exc}') from exc

            if resp.status_code == HTTPStatus.OK:
                embeddings = resp.output['embeddings']
                if len(embeddings) != len(inputs):
                    # 状态码 200 但数量对不上：绝不能静默返回错位/缺项的向量列表——
                    # 调用方按位置 zip(image_paths, vectors) 会把向量错误地绑到别的图片上，
                    # 这是静默的数据损坏（搜 A 出 B）。必须是一次响亮的失败。
                    logger.error(
                        'embedding.count_mismatch request_id=%s expected=%s actual=%s',
                        request_id, len(inputs), len(embeddings),
                    )
                    raise EmbeddingServiceError(
                        f'返回向量数量({len(embeddings)})与请求数量({len(inputs)})不符'
                    )
                embeddings = sorted(embeddings, key=lambda e: e.get('index', 0))
                logger.info(
                    'embedding.success request_id=%s count=%s latency_ms=%s',
                    request_id, len(embeddings), int((time.perf_counter() - start) * 1000),
                )
                return [np.array(e['embedding'], dtype=np.float32) for e in embeddings]

            message = getattr(resp, 'message', '') or ''
            if resp.status_code == HTTPStatus.TOO_MANY_REQUESTS:
                if attempt < self.max_retries:
                    logger.warning(
                        'embedding.retry request_id=%s attempt=%s delay_seconds=%s message=%s',
                        request_id, attempt, delay, message,
                    )
                    time.sleep(delay)
                    delay *= 2
                    continue

                logger.error(
                    'embedding.rate_limit_exhausted request_id=%s attempts=%s message=%s latency_ms=%s',
                    request_id, attempt, message,
                    int((time.perf_counter() - start) * 1000),
                )
                raise EmbeddingRateLimitExhaustedError(
                    f'429重试{self.max_retries}次后仍限流: {message}'
                )

            logger.error(
                'embedding.failed request_id=%s status=%s message=%s latency_ms=%s',
                request_id, resp.status_code, message,
                int((time.perf_counter() - start) * 1000),
            )
            raise EmbeddingServiceError(f'API调用失败({resp.status_code}): {message}')
