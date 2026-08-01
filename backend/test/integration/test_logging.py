"""结构化日志回归测试。

背景：`backend/app.py` 里此前没有任何 `logging.basicConfig`/`dictConfig`调用，
`services/` 下一律用模块级 `logging.getLogger(__name__)`——这类 logger 没有
显式设置级别，effective level 会一路继承到 root logger。Python 的 root logger
天然默认级别是 WARNING（30），所以在没有任何配置的情况下，
`services/vector_search.py` 里的 `logger.info('vector.search.success ...')`
从未真正被输出过（`isEnabledFor(INFO)` 在 record 创建之前就返回 False，
连 handler 都不会被调用）。

诊断记录（pytest 与 `logging.basicConfig` 的已知坑）：
pytest 自身的日志插件（`_pytest.logging`）在任何测试函数运行之前，就已经往
root logger 上挂了若干 handler（用于 caplog / 实时日志等，`pytest_configure`
阶段完成）。而 cpython 的 `logging.basicConfig()`（未传 `force=True`）在源码
里整个函数体（包括 `root.setLevel(level)`）都包在
`if len(root.handlers) == 0:` 内部——只要 root 已经有 handler，
`basicConfig()` 就是彻底的 no-op，连 level 都不会碰。这意味着：
1. 不能直接 `caplog.set_level(logging.INFO)` 断言——那个调用完全绕开
   `basicConfig`，直接给 logger/root 强制设置级别并接管 handler；哪怕
   `create_app()` 里的 `logging.basicConfig(...)` 那一行被删掉，测试也会
   照样通过，起不到回归保护作用（不允许用这种方式让测试变绿）。
2. 也不能简单依赖 `caplog.text`（不调用 `set_level`）——因为 pytest 在测试
   开始前就已经往 root 挂了 handler，`create_app()` 里的 `basicConfig` 调用
   在 pytest 进程里天然会被短路成 no-op，导致这个断言无论生产代码里有没有
   那一行 fix，结果都一样（一直失败），无法起到区分作用。

本测试的做法：在测试内手动清空 root 的 handlers 并把 level 打回 Python 的
天然默认值 WARNING，模拟一个"从未被任何框架配置过"的干净状态（贴近生产环境
gunicorn worker 冷启动、`create_app()` 第一次被调用时 root 尚无 handler 的
真实情形）。在这个干净状态下真实调用一次 `create_app()`——只有此时
`root.handlers` 确实为空，`basicConfig()` 才会真正走到 `setLevel` 分支。
随后用一个独立的 `logging.Handler`（不复用 caplog 的内部状态）接管 root，
通过真实的 `POST /api/products/search` 端点触发 `VectorSearchService`，
断言捕获到的日志文本里出现 `vector.search.success`。

`vector.search.success` 只会在 pgvector 查询真正执行成功后才被记录（SQL
里用了 `<=>` 运算符和 `CAST(... AS vector)`，SQLite 无法解析），因此本测试
必须放在 `test/integration/`（真实 PostgreSQL），而不能用 SQLite 内存库。
"""
import io
import logging

import numpy as np
from PIL import Image

from models import ImageAsset, db
from services.vector_search import VectorSearchService


class _FakeEmbeddingClient:
    """避免真实 DashScope 调用：无视图片内容，直接返回预先构造好的向量。"""

    def __init__(self, vector):
        self._vector = vector

    def embed_normalized_image(self, image_path, request_id=None):
        with Image.open(image_path) as image:
            assert image.format == 'JPEG'
        return self._vector


class _ListHandler(logging.Handler):
    """独立于 caplog 的最小 handler，只负责收集格式化后的日志文本。"""

    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_create_app_configures_logging_so_vector_search_success_is_emitted(app):
    vector = np.zeros(1024, dtype=np.float32)
    vector[0] = 1.0

    with app.app_context():
        db.session.add(ImageAsset(
            source_provider='qiniu-kodo',
            source_bucket='xiangxipackage',
            source_relative_path='日志/匹配图片.png',
            source_revision=1,
            oss_path='image-search/xiangxipackage/日志/匹配图片.png',
            preview_oss_path=(
                'image-search/previews/preview-v1/aa/'
                + 'a' * 64
                + '.jpg'
            ),
            content_hash='a' * 64,
            source_size=123,
            source_mime_type='image/png',
            source_width=8,
            source_height=8,
            vector=vector.tolist(),
            embedding_model='tongyi-embedding-vision-plus-2026-03-06',
            embedding_dimension=1024,
            normalization_version='preview-v1',
            status='active',
        ))
        db.session.commit()

    app.config['PRODUCT_SEARCH_SERVICE'] = VectorSearchService(
        embedding_client=_FakeEmbeddingClient(vector)
    )

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    for handler in saved_handlers:
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)  # Python 天然默认值，模拟未被任何框架配置过

    list_handler = _ListHandler()
    try:
        from app import create_app
        create_app('testing')  # 真实调用；只关心它对 root logger 的配置副作用

        root.addHandler(list_handler)

        query_image = io.BytesIO()
        Image.new('RGB', (8, 8), 'red').save(query_image, format='PNG')
        query_image.seek(0)
        client = app.test_client()
        response = client.post(
            '/api/products/search',
            data={'image': (query_image, 'q.png')},
            content_type='multipart/form-data',
        )

        assert response.status_code == 200
        assert response.get_json()[0]['relative_path'] == '日志/匹配图片.png'
        assert any('vector.search.success' in message for message in list_handler.messages), (
            f'未捕获到 vector.search.success，实际捕获到的日志: {list_handler.messages}'
        )
    finally:
        root.removeHandler(list_handler)
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
