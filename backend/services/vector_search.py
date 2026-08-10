"""独立图片资产的 pgvector 向量检索。"""
import logging
import tempfile
import time
from pathlib import Path

from sqlalchemy import text

from models import db
from services.embedding import EmbeddingClient, EmbeddingServiceError
from services.image_normalizer import ImageNormalizer

logger = logging.getLogger(__name__)


class VectorSearchError(Exception):
    """向量检索异常。"""


MIN_EF_SEARCH = 40       # pgvector 的 hnsw.ef_search 默认值

_SEARCH_SQL = text("""
SELECT id,
       model_number,
       display_name,
       source_relative_path,
       version,
       vector <=> CAST(:query_vector AS vector) AS distance
FROM image_assets
WHERE status = 'active'
ORDER BY vector <=> CAST(:query_vector AS vector)
LIMIT :top_k
""")


def _to_vector_literal(vector):
    """pgvector 文本字面量。用 repr 保留 float 全精度。"""
    return '[' + ','.join(str(float(x)) for x in vector) + ']'


class VectorSearchService:
    """无状态：不加载任何向量到内存，全部交给 PostgreSQL。"""

    def __init__(self, embedding_client=None, normalizer=None):
        self._embedding = embedding_client or EmbeddingClient()
        self._normalizer = normalizer or ImageNormalizer.from_env()

    def extract_feature(self, image_path, request_id=None):
        """标准化临时查询图并生成向量；规范化文件不会离开本次调用。"""
        with tempfile.TemporaryDirectory(prefix='image-query-') as temp_dir:
            normalized = self._normalizer.normalize(image_path)
            preview_path = Path(temp_dir) / 'query-preview.jpg'
            preview_path.write_bytes(normalized.data)
            return self._embedding.embed_normalized_image(
                str(preview_path),
                request_id=request_id,
            )

    def search_similar_images(self, image_path, top_k=10, request_id=None):
        query_vector = self.extract_feature(image_path, request_id=request_id)
        return self.search_by_vector(query_vector, top_k=top_k, request_id=request_id)

    def search_by_vector(self, vector, top_k=10, request_id=None):
        """按向量返回活跃图片资产 Top-K，每张资产独立占一个结果位置。

        契约（T3 fix round 1 补充）：本方法结束前（无论成功还是异常）都会对
        db.session 执行一次 rollback()——用于让 SET LOCAL 失效，把连接干净地
        还给连接池。调用方若在调用本方法之前，在同一个 session 里留有尚未
        commit 的写入（db.session.add 但未 commit），会被这次 rollback 静默
        丢弃且不报错；因此**禁止在同一请求内、调用本方法之前遗留未提交的写入**。
        """
        start = time.perf_counter()
        try:
            top_k = int(top_k)
            ef_search = max(top_k, MIN_EF_SEARCH)

            # SET LOCAL 而非 SET：Gunicorn + SQLAlchemy 会复用连接，
            # SET 会污染这条连接上后续所有查询。int() 已保证无注入风险。
            db.session.execute(text(f'SET LOCAL hnsw.ef_search = {int(ef_search)}'))

            rows = db.session.execute(_SEARCH_SQL, {
                'query_vector': _to_vector_literal(vector),
                'top_k': top_k,
            }).all()

            results = [{
                'asset_id': str(row.id),
                'model_number': row.model_number,
                'display_name': row.display_name,
                'source_relative_path': row.source_relative_path,
                'relative_path': row.source_relative_path,
                'version': row.version,
                'preview_url': f'/api/image-assets/{row.id}/preview',
                # 夹上界：实测向量 L2 范数 1.000282，同图余弦相似度会达到 1.00056
                'similarity': min(1.0, max(0.0, 1.0 - float(row.distance))),
            } for row in rows]

            logger.info(
                'vector.search.success request_id=%s top_k=%s ef_search=%s '
                'result_count=%s latency_ms=%s',
                request_id, top_k, ef_search, len(results),
                int((time.perf_counter() - start) * 1000),
            )
            return results
        except Exception as exc:
            logger.error(
                'vector.search.failed request_id=%s top_k=%s latency_ms=%s error=%s',
                request_id, top_k, int((time.perf_counter() - start) * 1000), exc,
            )
            raise VectorSearchError(f'向量检索失败: {exc}') from exc
        finally:
            # 结束事务，让 SET LOCAL 失效，连接干净地回到池里；
            # 同时也是上方 docstring 里说明的隐式清空点（只需一次，except 分支
            # 不再重复调用 rollback——finally 总会在异常传播前执行）。
            db.session.rollback()


__all__ = ['VectorSearchError', 'VectorSearchService', 'EmbeddingServiceError']
