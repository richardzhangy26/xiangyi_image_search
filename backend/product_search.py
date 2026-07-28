"""兼容层：真实实现已拆分到 backend/services/。

保留此模块是为了让 app.py 与既有测试的导入路径不变：
    from product_search import ImageSearchService, EmbeddingServiceError, VectorSearchError
新代码请直接从 services.embedding / services.vector_search 导入。
"""
from services.embedding import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    MAX_BATCH_SIZE,
    EmbeddingClient,
    EmbeddingServiceError,
)
from services.vector_search import VectorSearchError, VectorSearchService

# app.py:72 与既有测试仍使用这个名字
ImageSearchService = VectorSearchService

__all__ = [
    'EMBEDDING_DIMENSION',
    'EMBEDDING_MODEL',
    'MAX_BATCH_SIZE',
    'EmbeddingClient',
    'EmbeddingServiceError',
    'ImageSearchService',
    'VectorSearchError',
    'VectorSearchService',
]
