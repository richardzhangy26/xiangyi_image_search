"""pgvector 向量检索。

关键设计：在 SQL 内先过采样再用 DISTINCT ON 按 model_number 折叠，
而不是取 top_k 张图后在 Python 里折叠——后者会让「返回 N 个相似款」
退化成「返回 N 张相似图折叠后的剩余数量」。
"""
import logging
import os
import time

from sqlalchemy import text

from models import db
from services.embedding import EmbeddingClient, EmbeddingServiceError

logger = logging.getLogger(__name__)


class VectorSearchError(Exception):
    """向量检索异常。"""


DEFAULT_OVERSAMPLE = 5   # ≈ 单个产品的平均图片数
MAX_FETCH_N = 500
MIN_EF_SEARCH = 40       # pgvector 的 hnsw.ef_search 默认值

# CTE 用 MATERIALIZED 强制两阶段执行：先 HNSW 取 fetch_n 个候选，再折叠。
# 否则 PostgreSQL 12+ 可能内联 CTE，改变预期的执行形状。
_SEARCH_SQL = text("""
WITH candidates AS MATERIALIZED (
    SELECT model_number, image_path, original_path, oss_path,
           vector <=> CAST(:query_vector AS vector) AS distance
    FROM product_images
    ORDER BY vector <=> CAST(:query_vector AS vector)
    LIMIT :fetch_n
), best AS (
    SELECT DISTINCT ON (model_number)
           model_number, image_path, original_path, oss_path, distance
    FROM candidates
    ORDER BY model_number, distance
)
SELECT model_number, image_path, original_path, oss_path, distance
FROM best
ORDER BY distance
LIMIT :top_k
""")


def _oversample():
    try:
        value = int(os.getenv('SEARCH_OVERSAMPLE', DEFAULT_OVERSAMPLE))
    except ValueError:
        return DEFAULT_OVERSAMPLE
    return value if value >= 1 else DEFAULT_OVERSAMPLE


def _to_vector_literal(vector):
    """pgvector 文本字面量。用 repr 保留 float 全精度。"""
    return '[' + ','.join(str(float(x)) for x in vector) + ']'


class VectorSearchService:
    """无状态：不加载任何向量到内存，全部交给 PostgreSQL。"""

    def __init__(self, embedding_client=None):
        self._embedding = embedding_client or EmbeddingClient()

    def extract_feature(self, image_path, request_id=None):
        """保留此方法名以兼容 blueprints/products_v2.py 中既有调用。"""
        return self._embedding.embed_image(image_path, request_id=request_id)

    def search_similar_images(self, image_path, top_k=10, request_id=None):
        query_vector = self.extract_feature(image_path, request_id=request_id)
        return self.search_by_vector(query_vector, top_k=top_k, request_id=request_id)

    def search_by_vector(self, vector, top_k=10, request_id=None):
        start = time.perf_counter()
        top_k = int(top_k)
        fetch_n = max(top_k, min(top_k * _oversample(), MAX_FETCH_N))
        ef_search = max(fetch_n, MIN_EF_SEARCH)

        try:
            # SET LOCAL 而非 SET：Gunicorn + SQLAlchemy 会复用连接，
            # SET 会污染这条连接上后续所有查询。int() 已保证无注入风险。
            db.session.execute(text(f'SET LOCAL hnsw.ef_search = {int(ef_search)}'))

            rows = db.session.execute(_SEARCH_SQL, {
                'query_vector': _to_vector_literal(vector),
                'fetch_n': fetch_n,
                'top_k': top_k,
            }).all()

            results = [{
                'model_number': row.model_number,
                'image_path': row.image_path,
                'original_path': row.original_path,
                'oss_path': row.oss_path,
                # 夹上界：实测向量 L2 范数 1.000282，同图余弦相似度会达到 1.00056
                'similarity': min(1.0, max(0.0, 1.0 - float(row.distance))),
            } for row in rows]

            logger.info(
                'vector.search.success request_id=%s top_k=%s fetch_n=%s ef_search=%s '
                'result_count=%s latency_ms=%s',
                request_id, top_k, fetch_n, ef_search, len(results),
                int((time.perf_counter() - start) * 1000),
            )
            return results
        except Exception as exc:
            db.session.rollback()
            logger.error(
                'vector.search.failed request_id=%s top_k=%s latency_ms=%s error=%s',
                request_id, top_k, int((time.perf_counter() - start) * 1000), exc,
            )
            raise VectorSearchError(f'向量检索失败: {exc}') from exc
        finally:
            # 结束事务，让 SET LOCAL 失效，连接干净地回到池里
            db.session.rollback()


__all__ = ['VectorSearchError', 'VectorSearchService', 'EmbeddingServiceError']
