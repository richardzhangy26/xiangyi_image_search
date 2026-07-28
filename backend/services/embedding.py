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
        except Exception as exc:  # noqa: BLE001 - 批失败一律降级重试
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
                embeddings = sorted(resp.output['embeddings'], key=lambda e: e.get('index', 0))
                logger.info(
                    'embedding.success request_id=%s count=%s latency_ms=%s',
                    request_id, len(embeddings), int((time.perf_counter() - start) * 1000),
                )
                return [np.array(e['embedding'], dtype=np.float32) for e in embeddings]

            message = getattr(resp, 'message', '') or ''
            if resp.status_code == HTTPStatus.TOO_MANY_REQUESTS and attempt < self.max_retries:
                logger.warning(
                    'embedding.retry request_id=%s attempt=%s delay_seconds=%s message=%s',
                    request_id, attempt, delay, message,
                )
                time.sleep(delay)
                delay *= 2
                continue

            logger.error(
                'embedding.failed request_id=%s status=%s message=%s latency_ms=%s',
                request_id, resp.status_code, message,
                int((time.perf_counter() - start) * 1000),
            )
            raise EmbeddingServiceError(f'API调用失败({resp.status_code}): {message}')

        raise EmbeddingServiceError('图片向量提取失败: 重试次数已耗尽')
