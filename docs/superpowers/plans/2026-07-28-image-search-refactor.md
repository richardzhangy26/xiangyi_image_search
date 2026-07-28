# 以图搜款重构 + 批量导入 + 图片去重 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复以图搜款的检索语义缺陷，新增基于 SHA-256 的全库图片去重，并提供一个幂等的目录批量导入 CLI。

**Architecture:** 把 `backend/product_search.py` 里混在一起的四种职责（图片压缩、DashScope 调用、重试、向量检索）拆成 `backend/services/` 下的三个单一职责模块，`product_search.py` 缩为兼容层以免动 `app.py` 和现有测试。检索从「取 top_k 张图后在 Python 里折叠」改为「SQL 内过采样 + `DISTINCT ON` 折叠」。去重靠 `product_images.content_hash` 的全库 UNIQUE 约束，在调用 DashScope 之前拦截。

**Tech Stack:** Python 3.13 / Flask 3 / Flask-SQLAlchemy 3.1 / SQLAlchemy 2.0 / PostgreSQL 16 + pgvector 0.8.5 / DashScope `tongyi-embedding-vision-plus-2026-03-06` / pytest 8

**依据 spec:** `docs/superpowers/specs/2026-07-28-image-search-refactor-design.md`

## Global Constraints

- 向量模型固定为 `tongyi-embedding-vision-plus-2026-03-06`，`dimension=1024`，与数据库 `vector(1024)` 列一致。不得混入其他模型的向量。
- DashScope 单次请求内容元素**硬上限 20**（实测：传 32 返回 `400 contents count (32) exceeds limit (20)`）。
- 距离度量统一 **cosine**（`vector_cosine_ops` / `<=>`）。实测向量 L2 范数为 1.000282，非精确归一化，因此不得改用内积。
- `content_hash` 类型统一 `VARCHAR(64)`（不用 `CHAR(64)`），值为**源文件原始字节**的 SHA-256 十六进制小写。
- 唯一索引名统一 `uq_product_images_content_hash`，HNSW 索引名统一 `idx_product_images_vector_hnsw`。
- Schema 定义存在于三处，任何变更必须同步：`backend/models/product.py`（权威）、`postgres/init/01_init.sql`、`backend/init_db.py`。
- 所有回复、注释、日志文案用中文。
- 开发环境为 miniconda base（`~/miniconda3/bin/python`），依赖已装齐，**本计划不新增任何第三方依赖**。
- 所有 pytest 命令均在 `backend/` 目录下执行。
- 数据库当前为空（`products` 0 行、`product_images` 0 行），因此**不需要 `docker compose down -v`**，也不需要数据回填。

---

## File Structure

| 文件 | 职责 | 任务 |
|---|---|---|
| `backend/models/product.py` | SQLAlchemy 模型（schema 权威定义） | T1 |
| `postgres/init/01_init.sql` | Docker 首次启动建库 | T1 |
| `backend/init_db.py` | 已有库的幂等收敛 | T1 |
| `backend/test/integration/conftest.py` | 集成测试夹具（真 PostgreSQL） | T1 |
| `backend/services/__init__.py` | 空包标记 | T2 |
| `backend/services/embedding.py` | 图片压缩/base64、DashScope 单张+批量调用、429 重试、批失败降级 | T2 |
| `backend/services/vector_search.py` | pgvector 检索、`ef_search` 设置、相似度换算 | T3 |
| `backend/product_search.py` | 兼容层，只做再导出 | T3 |
| `backend/services/ingest.py` | 内容哈希、查重、落盘、入库（CLI 与 API 共用） | T4 |
| `backend/blueprints/products_v2.py` | HTTP 端点（检索改造 + bug 修复 + CSV 批量） | T5, T6, T7 |
| `backend/scripts/ingest_images.py` | 目录批量导入 CLI | T8 |
| `CLAUDE.md` | 项目文档 | T9 |

**依赖方向：** `ingest.py` → `embedding.py`；`vector_search.py` → `embedding.py`；三者 → `models`。无循环依赖。

**任务依赖：** T1 → T2 → {T3, T4} → {T5, T6, T7} → T8 → T9

---

## Task 1: content_hash 列与集成测试基建

**Files:**
- Modify: `backend/models/product.py:88-119`
- Modify: `postgres/init/01_init.sql:50-73`
- Modify: `backend/init_db.py`
- Create: `backend/test/integration/__init__.py`
- Create: `backend/test/integration/conftest.py`
- Test: `backend/test/integration/test_schema.py`

**Interfaces:**
- Consumes: 无
- Produces: `ProductImage.content_hash`（`VARCHAR(64)`，nullable，唯一索引 `uq_product_images_content_hash`）；pytest fixture `app`（后续所有集成测试依赖）

---

- [ ] **Step 1: 建集成测试包与 conftest**

集成测试连本机 5433 端口上的 Docker PostgreSQL，使用独立数据库 `image_search_test`，不污染开发数据。

`backend/app.py:13` 在**模块导入时**就读取了 `DATABASE_URL`，且 Flask-SQLAlchemy 3.1 在 `init_app()` 时即创建 engine，所以必须在 `import app` 之前设置环境变量。pytest 保证先导入目录下的 `conftest.py` 再导入测试模块，因此在 conftest 顶层设置即可。

创建空文件 `backend/test/integration/__init__.py`。

创建 `backend/test/integration/conftest.py`：

```python
"""集成测试夹具：连接本机 5433 上的 Docker PostgreSQL，使用独立测试库。

必须在任何 `import app` 之前设置 DATABASE_URL —— app.py 在模块导入时读取该变量，
且 Flask-SQLAlchemy 3.1 在 init_app() 阶段就创建了 engine。
"""
import os
import sys
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy import text

BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

TEST_DB_NAME = 'image_search_test'


def _dsn(database):
    user = os.getenv('DB_USER', 'postgres')
    password = os.getenv('DB_PASSWORD', '')
    host = os.getenv('DB_HOST', 'localhost')
    port = os.getenv('DB_PORT', '5433')
    return f'postgresql://{user}:{password}@{host}:{port}/{database}'


# 关键：必须在 import app 之前生效
os.environ['DATABASE_URL'] = _dsn(TEST_DB_NAME)


@pytest.fixture(scope='session')
def _test_database():
    """确保测试库存在；PostgreSQL 不可用时跳过整个集成测试套件。"""
    try:
        engine = sqlalchemy.create_engine(_dsn('postgres'), isolation_level='AUTOCOMMIT')
        with engine.connect() as conn:
            exists = conn.execute(
                text('SELECT 1 FROM pg_database WHERE datname = :name'),
                {'name': TEST_DB_NAME},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE {TEST_DB_NAME}'))
        engine.dispose()
    except Exception as exc:  # noqa: BLE001 - 任何连接失败都应跳过而非报错
        pytest.skip(f'PostgreSQL 不可用（{exc}），跳过集成测试')
    return _dsn(TEST_DB_NAME)


@pytest.fixture()
def app(_test_database, tmp_path):
    """每个测试一套干净的表结构。"""
    from app import create_app
    from models import db

    application = create_app()
    application.config['TESTING'] = True
    application.config['UPLOAD_FOLDER'] = str(tmp_path / 'uploads')
    os.makedirs(application.config['UPLOAD_FOLDER'], exist_ok=True)

    with application.app_context():
        db.session.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        db.session.commit()
        db.drop_all()
        db.create_all()
        yield application
        db.session.remove()
```

注意 `create_app()` 默认 `config_name='development'`，会走 `DATABASE_URL` 分支并初始化真实的 `ImageSearchService`（无状态，不产生 API 调用），这是我们想要的。

- [ ] **Step 2: 写失败的 schema 测试**

创建 `backend/test/integration/test_schema.py`：

```python
"""验证 content_hash 列存在且全库唯一。"""
import pytest
from sqlalchemy.exc import IntegrityError

from models import Product, ProductImage, db


def _add_product(model_number):
    db.session.add(Product(
        model_number=model_number,
        photographer_file='p',
        alibaba_product_url='https://example.com/1',
        category='相机肩带',
    ))
    db.session.commit()


def test_content_hash_column_accepts_value(app):
    _add_product('M-001')
    db.session.add(ProductImage(
        model_number='M-001',
        image_path='/uploads/product_images/M-001/aaaa.jpg',
        vector=[0.1] * 1024,
        content_hash='a' * 64,
    ))
    db.session.commit()

    row = ProductImage.query.one()
    assert row.content_hash == 'a' * 64
    assert row.to_dict()['content_hash'] == 'a' * 64


def test_duplicate_content_hash_rejected_across_different_products(app):
    """全库唯一：同一张图出现在两个型号下也必须被拒绝。"""
    _add_product('M-001')
    _add_product('M-002')

    db.session.add(ProductImage(
        model_number='M-001',
        image_path='/uploads/product_images/M-001/aaaa.jpg',
        vector=[0.1] * 1024,
        content_hash='b' * 64,
    ))
    db.session.commit()

    db.session.add(ProductImage(
        model_number='M-002',
        image_path='/uploads/product_images/M-002/aaaa.jpg',
        vector=[0.2] * 1024,
        content_hash='b' * 64,
    ))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_null_content_hash_allowed_multiple_times(app):
    """UNIQUE 索引允许多个 NULL —— 旧数据不会因为迁移而炸掉。"""
    _add_product('M-001')
    db.session.add(ProductImage(
        model_number='M-001', image_path='/uploads/a.jpg',
        vector=[0.1] * 1024, content_hash=None,
    ))
    db.session.add(ProductImage(
        model_number='M-001', image_path='/uploads/b.jpg',
        vector=[0.1] * 1024, content_hash=None,
    ))
    db.session.commit()

    assert ProductImage.query.count() == 2
```

- [ ] **Step 3: 运行测试确认失败**

```bash
cd backend && python -m pytest test/integration/test_schema.py -v
```

预期：3 个测试全部 FAIL，报 `TypeError: 'content_hash' is an invalid keyword argument for ProductImage`。

若报 `PostgreSQL 不可用` 而 skip，先执行 `docker compose up -d db` 再重试。

- [ ] **Step 4: 给模型加列**

修改 `backend/models/product.py`，在 `ProductImage` 类中 `vector` 列之后加入新列，并在类末尾加 `__table_args__`：

```python
    vector = db.Column(Vector(1024), nullable=False, comment='1024维图像向量')
    content_hash = db.Column(db.String(64), nullable=True, comment='源文件 SHA-256（全库唯一，用于精确去重）')
    original_path = db.Column(db.Text, nullable=True, comment='文件系统原始路径')
```

在 `product = db.relationship(...)` 之后加入：

```python
    # 唯一索引名与 postgres/init/01_init.sql、init_db.py 保持一致
    __table_args__ = (
        db.Index('uq_product_images_content_hash', 'content_hash', unique=True),
    )
```

> 用 `db.Index(..., unique=True)` 而非列上的 `unique=True`，是为了让索引名可控、三处定义能对齐。

在 `ProductImage.to_dict()` 的返回字典中，`'image_path'` 之后加入一行：

```python
            'content_hash': self.content_hash,
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && python -m pytest test/integration/test_schema.py -v
```

预期：3 passed。

- [ ] **Step 6: 同步 01_init.sql**

修改 `postgres/init/01_init.sql`：在 `CREATE TABLE product_images` 的 `vector` 行之后加入 `content_hash`：

```sql
    vector        vector(1024) NOT NULL,
    content_hash  VARCHAR(64),
    original_path TEXT,
```

在「4) 索引」小节的外键索引之后、HNSW 索引之前加入：

```sql
-- 内容哈希唯一索引（全库精确去重：同一张图只能入库一次）
CREATE UNIQUE INDEX IF NOT EXISTS uq_product_images_content_hash
    ON product_images (content_hash);
```

- [ ] **Step 7: 同步 init_db.py**

修改 `backend/init_db.py`，在 `db.create_all()` 之后、HNSW 索引之前插入一段。`create_all()` 只建不存在的表，不会给已存在的表加列，所以已有库需要显式 `ALTER`：

```python
        # 3. 已有库的幂等收敛：create_all() 不会给已存在的表补列
        db.session.execute(text(
            'ALTER TABLE product_images ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)'
        ))
        db.session.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_product_images_content_hash '
            'ON product_images (content_hash)'
        ))
        db.session.commit()
        print("content_hash 列与唯一索引已就绪！")
```

并把原来的注释序号 `# 3.` 改为 `# 4.`。

- [ ] **Step 8: 验证 init_db 幂等且开发库已收敛**

```bash
cd backend && python init_db.py && python init_db.py
```

预期：两次都成功打印全部四条信息，无报错（幂等）。

```bash
docker exec fashion-crm-db psql -U postgres -d image_search -c "\d product_images"
```

预期：输出中能看到 `content_hash | character varying(64)` 以及索引 `"uq_product_images_content_hash" UNIQUE, btree (content_hash)`。

- [ ] **Step 9: 确认既有测试未被破坏**

```bash
cd backend && python -m pytest test/test_products_v2_search_behaviors.py -v
```

预期：5 passed。

- [ ] **Step 10: 提交**

```bash
git add backend/models/product.py postgres/init/01_init.sql backend/init_db.py backend/test/integration/
git commit -m "feat(db): 新增 product_images.content_hash 全库唯一列与集成测试基建"
```

---

## Task 2: EmbeddingClient（批量 + 降级 + 429 重试）

**Files:**
- Create: `backend/services/__init__.py`
- Create: `backend/services/embedding.py`
- Test: `backend/test/test_embedding_batch.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `EMBEDDING_MODEL: str = "tongyi-embedding-vision-plus-2026-03-06"`
  - `EMBEDDING_DIMENSION: int = 1024`
  - `MAX_BATCH_SIZE: int = 20`
  - `EmbeddingServiceError(Exception)`
  - `EmbeddingClient.embed_image(image_path: str, request_id: str | None = None) -> np.ndarray`
  - `EmbeddingClient.embed_images(image_paths: list[str], request_id: str | None = None) -> list[np.ndarray | None]`（长度与入参一致，失败项为 `None`）
  - `EmbeddingClient.embed_text(content: str, request_id: str | None = None) -> np.ndarray`

---

- [ ] **Step 1: 写失败的测试**

创建空文件 `backend/services/__init__.py`。

创建 `backend/test/test_embedding_batch.py`：

```python
"""EmbeddingClient 的分批、降级与重试行为。全程 mock DashScope，不产生真实调用。"""
import sys
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from services.embedding import (  # noqa: E402
    MAX_BATCH_SIZE,
    EmbeddingClient,
    EmbeddingServiceError,
)


class FakeResponse:
    def __init__(self, count, status_code=HTTPStatus.OK, message=''):
        self.status_code = status_code
        self.message = message
        self.output = {
            'embeddings': [
                {'index': i, 'embedding': [0.1] * 1024} for i in range(count)
            ]
        }


@pytest.fixture(autouse=True)
def _stub_data_uri():
    """跳过真实图片读取，测试只关心调用编排。"""
    with patch('services.embedding._to_data_uri', side_effect=lambda p, **kw: f'data:image/jpeg;base64,{p}'):
        yield


def test_embed_images_splits_into_chunks_of_max_batch_size():
    paths = [f'/img/{i}.jpg' for i in range(45)]
    calls = []

    def fake_call(**kwargs):
        calls.append(len(kwargs['input']))
        return FakeResponse(len(kwargs['input']))

    with patch('dashscope.MultiModalEmbedding.call', side_effect=fake_call):
        vectors = EmbeddingClient(api_key='k').embed_images(paths)

    assert calls == [MAX_BATCH_SIZE, MAX_BATCH_SIZE, 5]
    assert len(vectors) == 45
    assert all(isinstance(v, np.ndarray) for v in vectors)


def test_batch_failure_falls_back_to_single_calls():
    """一批里有坏图会让整批 400；降级后只有坏图被标记为 None。"""
    paths = [f'/img/{i}.jpg' for i in range(3)]

    def fake_call(**kwargs):
        inputs = kwargs['input']
        if len(inputs) > 1:
            return FakeResponse(0, status_code=400, message='invalid image')
        if inputs[0]['image'].endswith('1.jpg'):
            return FakeResponse(0, status_code=400, message='invalid image')
        return FakeResponse(1)

    with patch('dashscope.MultiModalEmbedding.call', side_effect=fake_call):
        vectors = EmbeddingClient(api_key='k').embed_images(paths)

    assert len(vectors) == 3
    assert isinstance(vectors[0], np.ndarray)
    assert vectors[1] is None
    assert isinstance(vectors[2], np.ndarray)


def test_retries_on_429_then_succeeds():
    attempts = {'n': 0}

    def fake_call(**kwargs):
        attempts['n'] += 1
        if attempts['n'] == 1:
            return FakeResponse(0, status_code=429, message='Throttling.RateQuota')
        return FakeResponse(1)

    with patch('dashscope.MultiModalEmbedding.call', side_effect=fake_call), \
         patch('services.embedding.time.sleep') as sleep:
        vector = EmbeddingClient(api_key='k').embed_image('/img/a.jpg')

    assert attempts['n'] == 2
    assert vector.shape == (1024,)
    sleep.assert_called_once_with(5.0)


def test_does_not_retry_on_non_429_error():
    attempts = {'n': 0}

    def fake_call(**kwargs):
        attempts['n'] += 1
        return FakeResponse(0, status_code=400, message='invalid image')

    with patch('dashscope.MultiModalEmbedding.call', side_effect=fake_call):
        with pytest.raises(EmbeddingServiceError):
            EmbeddingClient(api_key='k').embed_image('/img/a.jpg')

    assert attempts['n'] == 1


def test_gives_up_after_max_retries_on_persistent_429():
    attempts = {'n': 0}

    def fake_call(**kwargs):
        attempts['n'] += 1
        return FakeResponse(0, status_code=429, message='Throttling.RateQuota')

    with patch('dashscope.MultiModalEmbedding.call', side_effect=fake_call), \
         patch('services.embedding.time.sleep') as sleep:
        with pytest.raises(EmbeddingServiceError):
            EmbeddingClient(api_key='k', max_retries=3).embed_image('/img/a.jpg')

    assert attempts['n'] == 3
    # 指数退避：5s, 10s（最后一次失败不再 sleep）
    assert [c.args[0] for c in sleep.call_args_list] == [5.0, 10.0]


def test_embed_images_empty_list_makes_no_call():
    with patch('dashscope.MultiModalEmbedding.call') as call:
        assert EmbeddingClient(api_key='k').embed_images([]) == []
    call.assert_not_called()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python -m pytest test/test_embedding_batch.py -v
```

预期：collection error，`ModuleNotFoundError: No module named 'services.embedding'`。

- [ ] **Step 3: 实现 EmbeddingClient**

创建 `backend/services/embedding.py`：

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && python -m pytest test/test_embedding_batch.py -v
```

预期：6 passed。

- [ ] **Step 5: 提交**

```bash
git add backend/services/__init__.py backend/services/embedding.py backend/test/test_embedding_batch.py
git commit -m "feat(services): 新增 EmbeddingClient，支持 20 张批量、批失败降级与 429 重试"
```

---

## Task 3: VectorSearchService（SQL 内折叠 + ef_search）

**Files:**
- Create: `backend/services/vector_search.py`
- Modify: `backend/product_search.py`（整文件替换为兼容层）
- Test: `backend/test/integration/test_vector_search.py`

**Interfaces:**
- Consumes: `services.embedding.EmbeddingClient`、`EmbeddingServiceError`
- Produces:
  - `VectorSearchError(Exception)`
  - `VectorSearchService.extract_feature(image_path, request_id=None) -> np.ndarray`
  - `VectorSearchService.search_similar_images(image_path, top_k=10, request_id=None) -> list[dict]`
  - `VectorSearchService.search_by_vector(vector, top_k=10, request_id=None) -> list[dict]`
  - 结果 dict 键：`model_number`、`image_path`、`original_path`、`oss_path`、`similarity`
  - `product_search.ImageSearchService`（= `VectorSearchService` 别名，保持 `app.py:72` 不变）

---

- [ ] **Step 1: 写失败的集成测试**

创建 `backend/test/integration/test_vector_search.py`：

```python
"""向量检索的语义正确性。此前这条路径零测试覆盖——SQLite 测试用 FakeSearchService
把整条链路 mock 掉了，而 SQLite 只是把 VECTOR(1024) 当未知类型名接受。
"""
import numpy as np
import pytest

from models import Product, ProductImage, db
from services.vector_search import VectorSearchService


def _unit_vector(seed):
    """构造归一化向量：第 seed 维为 1，其余为 0。彼此正交，距离可预期。"""
    v = np.zeros(1024, dtype=np.float32)
    v[seed] = 1.0
    return v


def _tilted_vector(seed, tilt):
    """在 _unit_vector(seed) 基础上掺入一点第 1023 维，制造可控的距离差。"""
    v = _unit_vector(seed)
    v[1023] = tilt
    return v / np.linalg.norm(v)


def _seed(model_numbers_to_vectors):
    for model_number, vectors in model_numbers_to_vectors.items():
        db.session.add(Product(
            model_number=model_number,
            photographer_file='p',
            alibaba_product_url='https://example.com/x',
            category='相机肩带',
        ))
        for i, vec in enumerate(vectors):
            db.session.add(ProductImage(
                model_number=model_number,
                image_path=f'/uploads/product_images/{model_number}/{i}.jpg',
                vector=vec.tolist(),
                content_hash=f'{model_number}-{i}'.ljust(64, '0'),
                image_order=i,
                is_primary=(i == 0),
            ))
    db.session.commit()


def test_returns_distinct_products_not_distinct_images(app):
    """核心修复：一个产品有 5 张图时，top_k=3 必须返回 3 个不同产品。

    旧实现取 top_k 张图再在 Python 里折叠，这里只会返回 1 个产品。
    """
    _seed({
        'M-001': [_tilted_vector(0, t) for t in (0.01, 0.02, 0.03, 0.04, 0.05)],
        'M-002': [_tilted_vector(1, 0.01)],
        'M-003': [_tilted_vector(2, 0.01)],
    })

    results = VectorSearchService().search_by_vector(_unit_vector(0), top_k=3)

    assert len(results) == 3
    assert len({r['model_number'] for r in results}) == 3
    assert results[0]['model_number'] == 'M-001'   # 与查询向量同轴，距离最小


def test_returns_best_matching_image_per_product(app):
    """折叠时保留该产品下距离最小的那张图。"""
    _seed({'M-001': [_tilted_vector(0, 0.5), _tilted_vector(0, 0.01)]})

    results = VectorSearchService().search_by_vector(_unit_vector(0), top_k=1)

    assert len(results) == 1
    # tilt=0.01 的是第 1 张（索引 1），与查询向量更接近
    assert results[0]['image_path'] == '/uploads/product_images/M-001/1.jpg'


def test_results_ordered_by_descending_similarity(app):
    _seed({
        'M-001': [_tilted_vector(0, 0.01)],
        'M-002': [_tilted_vector(1, 0.01)],
    })

    results = VectorSearchService().search_by_vector(_unit_vector(0), top_k=2)

    assert results[0]['model_number'] == 'M-001'
    assert results[0]['similarity'] > results[1]['similarity']


def test_similarity_clamped_to_unit_interval(app):
    """实测向量 L2 范数 1.000282，同图余弦相似度可达 1.00056，必须夹上界。"""
    vec = _tilted_vector(0, 0.01)
    _seed({'M-001': [vec]})

    results = VectorSearchService().search_by_vector(vec, top_k=1)

    assert 0.0 <= results[0]['similarity'] <= 1.0
    assert results[0]['similarity'] == pytest.approx(1.0, abs=1e-3)


def test_top_k_larger_than_default_ef_search_still_returns_all(app):
    """旧实现的阻断级缺陷：hnsw.ef_search 默认 40，top_k=50 拿不满。"""
    _seed({f'M-{i:03d}': [_tilted_vector(i, 0.01)] for i in range(60)})

    results = VectorSearchService().search_by_vector(_unit_vector(0), top_k=50)

    assert len(results) == 50
    assert len({r['model_number'] for r in results}) == 50


def test_empty_database_returns_empty_list(app):
    assert VectorSearchService().search_by_vector(_unit_vector(0), top_k=10) == []


def test_result_dict_shape(app):
    _seed({'M-001': [_tilted_vector(0, 0.01)]})

    result = VectorSearchService().search_by_vector(_unit_vector(0), top_k=1)[0]

    assert set(result) == {'model_number', 'image_path', 'original_path', 'oss_path', 'similarity'}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python -m pytest test/integration/test_vector_search.py -v
```

预期：collection error，`ModuleNotFoundError: No module named 'services.vector_search'`。

- [ ] **Step 3: 实现 VectorSearchService**

创建 `backend/services/vector_search.py`：

```python
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
```

- [ ] **Step 4: 把 product_search.py 换成兼容层**

用以下内容**整体替换** `backend/product_search.py`（原 232 行的实现已全部搬到 `services/`）：

```python
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
```

- [ ] **Step 5: 运行集成测试确认通过**

```bash
cd backend && python -m pytest test/integration/test_vector_search.py -v
```

预期：7 passed。

若 `test_top_k_larger_than_default_ef_search_still_returns_all` 失败且返回数不足 50，说明 `SET LOCAL` 未生效——检查它与 `_SEARCH_SQL` 是否在同一事务内（两次 `db.session.execute` 之间不能有 commit）。

- [ ] **Step 6: 确认既有测试与应用启动未被破坏**

```bash
cd backend && python -m pytest test/test_products_v2_search_behaviors.py test/test_embedding_batch.py -v
```

预期：11 passed（5 + 6）。

```bash
cd backend && python -c "from app import create_app; create_app(); print('app 启动 OK')"
```

预期：打印 `app 启动 OK`。

- [ ] **Step 7: 提交**

```bash
git add backend/services/vector_search.py backend/product_search.py backend/test/integration/test_vector_search.py
git commit -m "refactor(search): 检索改为 SQL 内过采样+DISTINCT ON 折叠，修复 ef_search 与相似度上界"
```

---

## Task 4: ImageIngestService（哈希去重 + 落盘 + 入库）

**Files:**
- Create: `backend/services/ingest.py`
- Test: `backend/test/integration/test_dedup.py`

**Interfaces:**
- Consumes: `services.embedding.EmbeddingClient`、`models.{db, Product, ProductImage}`
- Produces:
  - `ALLOWED_EXTENSIONS: set[str] = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}`
  - `hash_bytes(data: bytes) -> str`
  - `hash_file(path: str) -> str`
  - `storage_paths(upload_folder: str, model_number: str, content_hash: str, ext: str) -> tuple[str, str]`（返回 `(web_path, filesystem_path)`）
  - `find_existing_hashes(hashes: list[str]) -> dict[str, str]`（`{content_hash: 已存在的 image_path}`）
  - `PendingImage`（dataclass：`model_number`、`source_path`、`content_hash`、`image_order`、`is_primary`）
  - `IngestResult`（dataclass：`model_number`、`content_hash`、`status`、`image_path`、`duplicate_of`、`error`；`status ∈ {'created', 'duplicate', 'failed'}`）
  - `ImageIngestService.ingest_one(model_number, data: bytes, filename: str, upload_folder: str, image_order=0, is_primary=False, request_id=None) -> IngestResult`
  - `ImageIngestService.ingest_pending(pending: list[PendingImage], upload_folder: str, request_id=None) -> list[IngestResult]`

两个 ingest 方法都**只 `db.session.add`，不 commit**，事务由调用方控制。

---

- [ ] **Step 1: 写失败的集成测试**

创建 `backend/test/integration/test_dedup.py`：

```python
"""SHA-256 全库精确去重。"""
import hashlib
import io
import os

import numpy as np
import pytest
from PIL import Image

from models import Product, ProductImage, db
from services.ingest import (
    ImageIngestService,
    PendingImage,
    hash_bytes,
    hash_file,
    storage_paths,
)


def _png_bytes(color):
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), color).save(buffer, format='PNG')
    return buffer.getvalue()


class FakeEmbedding:
    """记录调用次数，用于验证去重确实省下了 API 调用。"""

    def __init__(self):
        self.image_calls = 0
        self.batch_calls = 0

    def embed_image(self, image_path, request_id=None):
        self.image_calls += 1
        return np.full(1024, 0.1, dtype=np.float32)

    def embed_images(self, image_paths, request_id=None):
        self.batch_calls += 1
        self.image_calls += len(image_paths)
        return [np.full(1024, 0.1, dtype=np.float32) for _ in image_paths]


def _add_product(model_number):
    db.session.add(Product(
        model_number=model_number, photographer_file='p',
        alibaba_product_url='https://example.com/x', category='相机肩带',
    ))
    db.session.commit()


def test_hash_bytes_matches_hashlib():
    data = b'hello'
    assert hash_bytes(data) == hashlib.sha256(data).hexdigest()


def test_hash_file_matches_hash_bytes(tmp_path):
    data = _png_bytes('red')
    path = tmp_path / 'a.png'
    path.write_bytes(data)
    assert hash_file(str(path)) == hash_bytes(data)


def test_storage_paths_uses_hash_prefix_not_uuid():
    web, fs = storage_paths('/srv/uploads', 'CS-001', 'a' * 64, '.jpg')
    assert web == '/uploads/product_images/CS-001/aaaaaaaaaaaaaaaa.jpg'
    assert fs == '/srv/uploads/product_images/CS-001/aaaaaaaaaaaaaaaa.jpg'


def test_ingest_one_creates_row_and_file(app):
    _add_product('CS-001')
    embedding = FakeEmbedding()
    service = ImageIngestService(embedding_client=embedding)
    data = _png_bytes('red')

    result = service.ingest_one(
        'CS-001', data, '1.png', app.config['UPLOAD_FOLDER'], image_order=0, is_primary=True,
    )
    db.session.commit()

    assert result.status == 'created'
    assert result.content_hash == hash_bytes(data)
    assert ProductImage.query.count() == 1
    row = ProductImage.query.one()
    assert row.content_hash == hash_bytes(data)
    assert row.is_primary is True
    _, fs_path = storage_paths(app.config['UPLOAD_FOLDER'], 'CS-001', row.content_hash, '.png')
    with open(fs_path, 'rb') as handle:
        assert handle.read() == data
    assert embedding.image_calls == 1


def test_second_upload_of_same_bytes_is_skipped_without_api_call(app):
    """这正是磁盘上那 4 个同哈希文件的场景。"""
    _add_product('CS-001')
    embedding = FakeEmbedding()
    service = ImageIngestService(embedding_client=embedding)
    data = _png_bytes('red')

    service.ingest_one('CS-001', data, '1.png', app.config['UPLOAD_FOLDER'])
    db.session.commit()
    result = service.ingest_one('CS-001', data, '副本.png', app.config['UPLOAD_FOLDER'])
    db.session.commit()

    assert result.status == 'duplicate'
    assert result.duplicate_of == '/uploads/product_images/CS-001/' + hash_bytes(data)[:16] + '.png'
    assert ProductImage.query.count() == 1
    assert embedding.image_calls == 1  # 第二次没调 API


def test_dedup_is_global_across_products(app):
    """全库唯一：同一张图出现在另一个型号下也算重复。"""
    _add_product('CS-001')
    _add_product('HL-002')
    embedding = FakeEmbedding()
    service = ImageIngestService(embedding_client=embedding)
    data = _png_bytes('red')

    service.ingest_one('CS-001', data, '1.png', app.config['UPLOAD_FOLDER'])
    db.session.commit()
    result = service.ingest_one('HL-002', data, '主图.png', app.config['UPLOAD_FOLDER'])
    db.session.commit()

    assert result.status == 'duplicate'
    assert 'CS-001' in result.duplicate_of
    assert ProductImage.query.count() == 1


def test_different_images_both_ingested(app):
    _add_product('CS-001')
    service = ImageIngestService(embedding_client=FakeEmbedding())

    service.ingest_one('CS-001', _png_bytes('red'), '1.png', app.config['UPLOAD_FOLDER'])
    service.ingest_one('CS-001', _png_bytes('blue'), '2.png', app.config['UPLOAD_FOLDER'])
    db.session.commit()

    assert ProductImage.query.count() == 2


def test_ingest_one_removes_file_when_embedding_fails(app):
    """向量生成失败不能留下孤儿文件。"""
    from services.embedding import EmbeddingServiceError

    _add_product('CS-001')

    class FailingEmbedding:
        def embed_image(self, image_path, request_id=None):
            raise EmbeddingServiceError('boom')

    service = ImageIngestService(embedding_client=FailingEmbedding())
    data = _png_bytes('red')

    with pytest.raises(EmbeddingServiceError):
        service.ingest_one('CS-001', data, '1.png', app.config['UPLOAD_FOLDER'])

    db.session.rollback()
    _, fs_path = storage_paths(app.config['UPLOAD_FOLDER'], 'CS-001', hash_bytes(data), '.png')
    assert not os.path.exists(fs_path)


def test_ingest_pending_deduplicates_within_the_same_batch(app, tmp_path):
    """同一次运行里出现两份相同内容，只入库一次。"""
    _add_product('CS-001')
    data = _png_bytes('red')
    first = tmp_path / 'a.png'
    second = tmp_path / 'b.png'
    first.write_bytes(data)
    second.write_bytes(data)

    embedding = FakeEmbedding()
    service = ImageIngestService(embedding_client=embedding)
    digest = hash_bytes(data)
    pending = [
        PendingImage('CS-001', str(first), digest, 0, True),
        PendingImage('CS-001', str(second), digest, 1, False),
    ]

    results = service.ingest_pending(pending, app.config['UPLOAD_FOLDER'])
    db.session.commit()

    assert [r.status for r in results] == ['created', 'duplicate']
    assert ProductImage.query.count() == 1
    assert embedding.image_calls == 1


def test_ingest_pending_marks_failed_when_vector_is_none(app, tmp_path):
    _add_product('CS-001')
    path = tmp_path / 'a.png'
    path.write_bytes(_png_bytes('red'))

    class NoneEmbedding:
        def embed_images(self, image_paths, request_id=None):
            return [None] * len(image_paths)

    service = ImageIngestService(embedding_client=NoneEmbedding())
    pending = [PendingImage('CS-001', str(path), 'c' * 64, 0, True)]

    results = service.ingest_pending(pending, app.config['UPLOAD_FOLDER'])
    db.session.commit()

    assert results[0].status == 'failed'
    assert ProductImage.query.count() == 0


def test_find_existing_hashes_chunks_large_input(app):
    """> 1000 个哈希要分块查询，不能一次塞进 IN 子句。"""
    from services.ingest import find_existing_hashes

    _add_product('CS-001')
    db.session.add(ProductImage(
        model_number='CS-001', image_path='/uploads/a.png',
        vector=[0.1] * 1024, content_hash='d' * 64,
    ))
    db.session.commit()

    probe = [f'{i:064d}' for i in range(2500)] + ['d' * 64]
    found = find_existing_hashes(probe)

    assert found == {'d' * 64: '/uploads/a.png'}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python -m pytest test/integration/test_dedup.py -v
```

预期：collection error，`ModuleNotFoundError: No module named 'services.ingest'`。

- [ ] **Step 3: 实现 ImageIngestService**

创建 `backend/services/ingest.py`：

```python
"""图片入库：内容哈希去重 → 落盘 → 生成向量 → 写表。

CLI（scripts/ingest_images.py）与 HTTP 端点（blueprints/products_v2.py）共用本模块。
所有方法只 db.session.add，不 commit —— 事务边界由调用方掌握。
"""
import hashlib
import logging
import os
from dataclasses import dataclass

from models import ProductImage, db
from services.embedding import EmbeddingClient

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}

# IN 子句一次塞太多参数会拖慢查询，分块处理
_HASH_QUERY_CHUNK = 1000

# 文件名只取哈希前 16 个十六进制字符（64 bit）。唯一性由库里
# 完整 64 字符哈希的 UNIQUE 约束保证，截断只影响可读性。
_FILENAME_HASH_LEN = 16


@dataclass
class PendingImage:
    """CLI 扫描阶段产出的待入库项。"""
    model_number: str
    source_path: str
    content_hash: str
    image_order: int
    is_primary: bool


@dataclass
class IngestResult:
    model_number: str
    content_hash: str
    status: str                      # 'created' | 'duplicate' | 'failed'
    image_path: str = None
    duplicate_of: str = None
    error: str = None
    source_path: str = None


def hash_bytes(data):
    """源文件原始字节的 SHA-256（十六进制小写）。"""
    return hashlib.sha256(data).hexdigest()


def hash_file(path):
    """流式计算文件哈希，避免大图占内存。"""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_ext(filename):
    """取小写扩展名；未知扩展名回退为 .jpg。"""
    ext = os.path.splitext(filename)[1].lower()
    return ext if ext in ALLOWED_EXTENSIONS else '.jpg'


def storage_paths(upload_folder, model_number, content_hash, ext):
    """哈希命名，天然幂等：同一张图永远落在同一路径。

    返回 (web_path, filesystem_path)。
    """
    relative = f'product_images/{model_number}/{content_hash[:_FILENAME_HASH_LEN]}{ext}'
    return f'/uploads/{relative}', os.path.join(upload_folder, relative)


def find_existing_hashes(hashes):
    """返回 {content_hash: 已存在的 image_path}，只包含库里已有的。"""
    if not hashes:
        return {}

    unique = list({h for h in hashes if h})
    found = {}
    for start in range(0, len(unique), _HASH_QUERY_CHUNK):
        chunk = unique[start:start + _HASH_QUERY_CHUNK]
        rows = db.session.query(
            ProductImage.content_hash, ProductImage.image_path
        ).filter(ProductImage.content_hash.in_(chunk)).all()
        found.update({content_hash: image_path for content_hash, image_path in rows})
    return found


class ImageIngestService:
    def __init__(self, embedding_client=None):
        self._embedding = embedding_client or EmbeddingClient()

    def ingest_one(self, model_number, data, filename, upload_folder,
                   image_order=0, is_primary=False, request_id=None):
        """单张入库（HTTP 上传路径）。重复返回 duplicate，不抛异常。"""
        content_hash = hash_bytes(data)

        existing = find_existing_hashes([content_hash])
        if content_hash in existing:
            logger.info(
                'ingest.duplicate model_number=%s content_hash=%s duplicate_of=%s',
                model_number, content_hash, existing[content_hash],
            )
            return IngestResult(
                model_number=model_number, content_hash=content_hash,
                status='duplicate', duplicate_of=existing[content_hash],
            )

        ext = normalized_ext(filename)
        web_path, fs_path = storage_paths(upload_folder, model_number, content_hash, ext)
        os.makedirs(os.path.dirname(fs_path), exist_ok=True)
        with open(fs_path, 'wb') as handle:
            handle.write(data)

        try:
            vector = self._embedding.embed_image(fs_path, request_id=request_id)
        except Exception:
            _remove_quietly(fs_path)   # 不留孤儿文件
            raise

        db.session.add(ProductImage(
            model_number=model_number,
            image_path=web_path,
            vector=vector.tolist(),
            content_hash=content_hash,
            original_path=fs_path,
            image_order=image_order,
            is_primary=is_primary,
        ))
        return IngestResult(
            model_number=model_number, content_hash=content_hash,
            status='created', image_path=web_path,
        )

    def ingest_pending(self, pending, upload_folder, request_id=None):
        """批量入库（CLI 路径）。调用方需保证 pending 长度 <= EmbeddingClient 的批大小。

        库内去重由调用方预先过滤；这里只处理批内重复。
        """
        if not pending:
            return []

        results = [None] * len(pending)
        seen_in_batch = {}
        to_embed = []          # [(下标, PendingImage)]

        for index, item in enumerate(pending):
            if item.content_hash in seen_in_batch:
                results[index] = IngestResult(
                    model_number=item.model_number, content_hash=item.content_hash,
                    status='duplicate', duplicate_of=seen_in_batch[item.content_hash],
                    source_path=item.source_path,
                )
                continue
            to_embed.append((index, item))
            ext = normalized_ext(item.source_path)
            web_path, _ = storage_paths(upload_folder, item.model_number, item.content_hash, ext)
            seen_in_batch[item.content_hash] = web_path

        if not to_embed:
            return results

        vectors = self._embedding.embed_images(
            [item.source_path for _, item in to_embed], request_id=request_id
        )

        for (index, item), vector in zip(to_embed, vectors):
            if vector is None:
                results[index] = IngestResult(
                    model_number=item.model_number, content_hash=item.content_hash,
                    status='failed', error='向量生成失败', source_path=item.source_path,
                )
                continue

            ext = normalized_ext(item.source_path)
            web_path, fs_path = storage_paths(
                upload_folder, item.model_number, item.content_hash, ext
            )
            os.makedirs(os.path.dirname(fs_path), exist_ok=True)
            with open(item.source_path, 'rb') as src, open(fs_path, 'wb') as dst:
                dst.write(src.read())

            db.session.add(ProductImage(
                model_number=item.model_number,
                image_path=web_path,
                vector=vector.tolist(),
                content_hash=item.content_hash,
                original_path=os.path.abspath(item.source_path),
                image_order=item.image_order,
                is_primary=item.is_primary,
            ))
            results[index] = IngestResult(
                model_number=item.model_number, content_hash=item.content_hash,
                status='created', image_path=web_path, source_path=item.source_path,
            )

        return results


def _remove_quietly(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        logger.warning('清理文件失败 path=%s error=%s', path, exc)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && python -m pytest test/integration/test_dedup.py -v
```

预期：11 passed。

- [ ] **Step 5: 提交**

```bash
git add backend/services/ingest.py backend/test/integration/test_dedup.py
git commit -m "feat(services): 新增 ImageIngestService，SHA-256 全库精确去重与哈希命名落盘"
```

---

## Task 5: 检索端点接入新服务

**Files:**
- Modify: `backend/blueprints/products_v2.py:48-60`（删除 `dedupe_results_by_model_number`）
- Modify: `backend/blueprints/products_v2.py:613-687`（`search_products`）
- Modify: `backend/test/test_products_v2_search_behaviors.py:118-141`（契约变更，改写该测试）

**Interfaces:**
- Consumes: `VectorSearchService.search_similar_images`（T3）
- Produces: `POST /api/products/search` 返回体不变（产品字典 + `similarity` + `matched_image`）

---

- [ ] **Step 1: 改写既有的 dedup 测试以反映新契约**

去重已下沉到 SQL，端点不再做应用层折叠——服务返回什么就用什么。把 `backend/test/test_products_v2_search_behaviors.py` 中的 `test_search_dedupes_model_number` 整个函数替换为：

```python
def test_search_returns_service_results_verbatim():
    """去重已下沉到 SQL（VectorSearchService 内的 DISTINCT ON），
    端点不再做应用层折叠。折叠正确性由 test/integration/test_vector_search.py 覆盖。
    """
    app, client = _build_client_with_db()
    _seed_products(app)
    app.config['PRODUCT_SEARCH_SERVICE'] = FakeSearchService(
        results=[
            {'model_number': 'M-001', 'image_path': '/uploads/a.jpg', 'similarity': 0.95},
            {'model_number': 'M-002', 'image_path': '/uploads/c.jpg', 'similarity': 0.75},
        ]
    )

    response = client.post(
        '/api/products/search',
        data={'image': (io.BytesIO(b'img'), 'a.jpg'), 'top_k': '10'},
        content_type='multipart/form-data',
    )

    assert response.status_code == 200
    body = response.get_json()
    assert len(body) == 2
    assert body[0]['model_number'] == 'M-001'
    assert body[0]['matched_image'] == '/uploads/a.jpg'
    assert body[0]['similarity'] == 0.95
    assert body[1]['model_number'] == 'M-002'


def test_search_preserves_service_result_order():
    """端点必须保持服务返回的顺序（已按距离升序），不得被字典查询打乱。"""
    app, client = _build_client_with_db()
    _seed_products(app)
    app.config['PRODUCT_SEARCH_SERVICE'] = FakeSearchService(
        results=[
            {'model_number': 'M-002', 'image_path': '/uploads/c.jpg', 'similarity': 0.91},
            {'model_number': 'M-001', 'image_path': '/uploads/a.jpg', 'similarity': 0.42},
        ]
    )

    response = client.post(
        '/api/products/search',
        data={'image': (io.BytesIO(b'img'), 'a.jpg'), 'top_k': '10'},
        content_type='multipart/form-data',
    )

    body = response.get_json()
    assert [item['model_number'] for item in body] == ['M-002', 'M-001']
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python -m pytest test/test_products_v2_search_behaviors.py -v
```

预期：`test_search_returns_service_results_verbatim` PASS（新契约恰好兼容旧实现），
`test_search_preserves_service_result_order` PASS。

若两个都已通过，仍继续 Step 3——目的是删掉冗余的应用层去重，测试是护栏而非驱动。

- [ ] **Step 3: 删除应用层去重函数**

删除 `backend/blueprints/products_v2.py` 中整个 `dedupe_results_by_model_number` 函数（第 48-60 行，含其上方空行）。

- [ ] **Step 4: 改写 search_products 中的结果组装**

在 `search_products` 内，把这一段：

```python
            deduped_results = dedupe_results_by_model_number(results)

            # 获取产品详情
            model_numbers = [result.get('model_number') for result in deduped_results]
```

改为：

```python
            # 去重已在 VectorSearchService 的 SQL（DISTINCT ON）内完成
            model_numbers = [result.get('model_number') for result in results]
```

并把下方循环的 `for result in deduped_results:` 改为 `for result in results:`。

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && python -m pytest test/test_products_v2_search_behaviors.py -v
```

预期：6 passed。

- [ ] **Step 6: 端到端手工验证**

```bash
docker compose up -d db
cd backend && python init_db.py && python app.py
```

另开一个终端：

```bash
curl -s -X POST http://localhost:5000/api/products/search \
  -F "image=@backend/uploads/product_images/cs-01/39111dbd-6cf9-46aa-acb2-0155897be6dc_1.png" \
  -F "top_k=10" | head -c 300
```

预期：库为空时返回 `[]`；不应出现 500 或 traceback。检查 `python app.py` 的输出中有一行
`vector.search.success ... top_k=10 fetch_n=50 ef_search=50 result_count=0`。

- [ ] **Step 7: 提交**

```bash
git add backend/blueprints/products_v2.py backend/test/test_products_v2_search_behaviors.py
git commit -m "refactor(api): 移除应用层去重，检索折叠统一由 SQL 完成"
```

---

## Task 6: 写入与删除路径的四处缺陷修复

**Files:**
- Modify: `backend/blueprints/products_v2.py:63-89`（`save_product_image` 删除，改用 ingest 服务）
- Modify: `backend/blueprints/products_v2.py:173-262`（`create_product`）
- Modify: `backend/blueprints/products_v2.py:265-326`（`update_product`）
- Modify: `backend/blueprints/products_v2.py:329-350`（`delete_product`）
- Modify: `backend/blueprints/products_v2.py:353-385`（`batch_delete_products`）
- Modify: `backend/blueprints/products_v2.py:392-421`（`delete_product_image`）
- Test: `backend/test/integration/test_write_paths.py`

**Interfaces:**
- Consumes: `services.ingest.{ImageIngestService, IngestResult, ALLOWED_EXTENSIONS}`（T4）
- Produces:
  - `POST /api/products` 响应新增 `skipped_duplicates: list[dict]`
  - `PUT /api/products/<model_number>` 响应新增 `uploaded_images: int`、`skipped_duplicates: list[dict]`；向量失败时返回 503
  - 删除类端点在提交成功后清理磁盘文件

---

- [ ] **Step 1: 写失败的集成测试**

创建 `backend/test/integration/test_write_paths.py`：

```python
"""写入与删除路径：不吞异常、清理磁盘文件、重复图片明确提示。"""
import io
import json
import os

import numpy as np
from PIL import Image

from models import Product, ProductImage, db
from services.embedding import EmbeddingServiceError


def _png_bytes(color):
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), color).save(buffer, format='PNG')
    return buffer.getvalue()


class FakeEmbedding:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def embed_image(self, image_path, request_id=None):
        self.calls += 1
        if self.fail:
            raise EmbeddingServiceError('boom')
        return np.full(1024, 0.1, dtype=np.float32)

    def embed_images(self, image_paths, request_id=None):
        return [self.embed_image(p, request_id) for p in image_paths]


def _install_embedding(app, embedding):
    from services.vector_search import VectorSearchService
    app.config['PRODUCT_SEARCH_SERVICE'] = VectorSearchService(embedding_client=embedding)
    app.config['IMAGE_INGEST_EMBEDDING'] = embedding


def _product_payload(model_number):
    return json.dumps({
        'model_number': model_number,
        'photographer_file': 'p',
        'alibaba_product_url': 'https://example.com/x',
        'category': '相机肩带',
    })


def test_create_product_reports_duplicate_images(app):
    _install_embedding(app, FakeEmbedding())
    client = app.test_client()
    data = _png_bytes('red')

    response = client.post('/api/products', data={
        'product': _product_payload('CS-001'),
        'images': [(io.BytesIO(data), '1.png'), (io.BytesIO(data), '副本.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 201
    body = response.get_json()
    assert body['uploaded_images'] == 1
    assert len(body['skipped_duplicates']) == 1
    assert ProductImage.query.count() == 1


def test_update_product_returns_503_when_embedding_fails(app):
    """旧行为：只 log，返回 200「更新成功」，图片文件留在磁盘上，数据静默丢失。"""
    _install_embedding(app, FakeEmbedding())
    client = app.test_client()
    client.post('/api/products', data={'product': _product_payload('CS-001')},
                content_type='multipart/form-data')

    _install_embedding(app, FakeEmbedding(fail=True))
    response = client.put('/api/products/CS-001', data={
        'images': [(io.BytesIO(_png_bytes('red')), '1.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 503
    assert response.get_json()['error_code'] == 'EMBEDDING_SERVICE_ERROR'
    assert ProductImage.query.count() == 0

    # 失败的图片文件不得留在磁盘上
    product_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'product_images', 'CS-001')
    assert not os.path.isdir(product_dir) or os.listdir(product_dir) == []


def test_delete_product_removes_files_from_disk(app):
    _install_embedding(app, FakeEmbedding())
    client = app.test_client()
    client.post('/api/products', data={
        'product': _product_payload('CS-001'),
        'images': [(io.BytesIO(_png_bytes('red')), '1.png')],
    }, content_type='multipart/form-data')

    fs_path = ProductImage.query.one().original_path
    assert os.path.exists(fs_path)

    response = client.delete('/api/products/CS-001')

    assert response.status_code == 200
    assert ProductImage.query.count() == 0
    assert not os.path.exists(fs_path)


def test_batch_delete_removes_files_from_disk(app):
    _install_embedding(app, FakeEmbedding())
    client = app.test_client()
    for model_number, color in (('CS-001', 'red'), ('CS-002', 'blue')):
        client.post('/api/products', data={
            'product': _product_payload(model_number),
            'images': [(io.BytesIO(_png_bytes(color)), '1.png')],
        }, content_type='multipart/form-data')

    paths = [row.original_path for row in ProductImage.query.all()]
    assert len(paths) == 2 and all(os.path.exists(p) for p in paths)

    response = client.post('/api/products/batch-delete',
                           json={'model_numbers': ['CS-001', 'CS-002']})

    assert response.status_code == 200
    assert response.get_json()['deleted_count'] == 2
    assert ProductImage.query.count() == 0
    assert not any(os.path.exists(p) for p in paths)


def test_delete_single_image_removes_file(app):
    _install_embedding(app, FakeEmbedding())
    client = app.test_client()
    client.post('/api/products', data={
        'product': _product_payload('CS-001'),
        'images': [(io.BytesIO(_png_bytes('red')), '1.png')],
    }, content_type='multipart/form-data')

    row = ProductImage.query.one()
    fs_path = row.original_path

    response = client.delete(f'/api/products/CS-001/images/{row.id}')

    assert response.status_code == 200
    assert not os.path.exists(fs_path)


def test_reupload_after_delete_is_allowed(app):
    """删除释放了哈希，同一张图可以重新上传。"""
    _install_embedding(app, FakeEmbedding())
    client = app.test_client()
    data = _png_bytes('red')
    client.post('/api/products', data={
        'product': _product_payload('CS-001'),
        'images': [(io.BytesIO(data), '1.png')],
    }, content_type='multipart/form-data')

    row_id = ProductImage.query.one().id
    client.delete(f'/api/products/CS-001/images/{row_id}')

    response = client.put('/api/products/CS-001', data={
        'images': [(io.BytesIO(data), '1.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    assert ProductImage.query.count() == 1
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python -m pytest test/integration/test_write_paths.py -v
```

预期：多个 FAIL。`test_update_product_returns_503_when_embedding_fails` 会返回 200 而非 503；
删除类测试会因为文件仍存在而 FAIL；`test_create_product_reports_duplicate_images` 会因为
响应缺少 `skipped_duplicates` 键而 KeyError。

- [ ] **Step 3: 替换辅助函数**

在 `backend/blueprints/products_v2.py` 顶部，把导入改为：

```python
from models import db, Product, ProductImage
from product_search import EmbeddingServiceError, VectorSearchError
from services.ingest import ALLOWED_EXTENSIONS, ImageIngestService
```

删除整个 `save_product_image` 函数（第 63-89 行），并把 `allowed_file` 改为复用共享常量：

```python
def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and \
        os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS
```

在 `allowed_file` 之后新增两个辅助函数：

```python
def get_ingest_service():
    """构造 ImageIngestService，复用测试注入的 embedding client（如果有）。"""
    return ImageIngestService(embedding_client=current_app.config.get('IMAGE_INGEST_EMBEDDING'))


def remove_files_quietly(paths):
    """删除磁盘文件；单个失败只记日志，不影响其余。"""
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            current_app.logger.warning(f"清理图片文件失败: {path}, error={exc}")
```

- [ ] **Step 4: 重写 create_product 的图片处理段**

把 `create_product` 中从 `# 处理图片上传` 到 `db.session.commit()` 之前的整段替换为：

```python
        # 处理图片上传
        ingest_service = get_ingest_service()
        images = request.files.getlist('images')
        uploaded_images = []
        skipped_duplicates = []
        request_id = uuid.uuid4().hex

        for idx, image_file in enumerate(images):
            if not image_file or not allowed_file(image_file.filename):
                continue

            data = image_file.read()
            result = ingest_service.ingest_one(
                model_number, data, image_file.filename,
                current_app.config['UPLOAD_FOLDER'],
                image_order=idx, is_primary=(idx == 0),
                request_id=request_id,
            )
            if result.status == 'created':
                saved_filesystem_paths.append(
                    os.path.join(
                        current_app.config['UPLOAD_FOLDER'],
                        result.image_path.removeprefix('/uploads/'),
                    )
                )
                uploaded_images.append(result.image_path)
            elif result.status == 'duplicate':
                skipped_duplicates.append({
                    'filename': image_file.filename,
                    'duplicate_of': result.duplicate_of,
                })
```

把返回体改为：

```python
        return jsonify({
            'message': '产品创建成功',
            'model_number': model_number,
            'uploaded_images': len(uploaded_images),
            'skipped_duplicates': skipped_duplicates
        }), 201
```

- [ ] **Step 5: 重写 update_product**

把 `update_product` 整个函数替换为：

```python
@products_v2_bp.route('/<model_number>', methods=['PUT'])
@cross_origin()
def update_product(model_number):
    """更新产品信息"""
    saved_filesystem_paths = []
    should_cleanup_files = False
    try:
        product = Product.query.get(model_number)
        if not product:
            return jsonify({'error': '产品不存在'}), 404

        # 获取更新数据
        product_data_str = request.form.get('product')
        if product_data_str:
            product_data = json.loads(product_data_str)

            # 更新字段（排除主键）
            for key, value in product_data.items():
                if key != 'model_number' and hasattr(product, key):
                    setattr(product, key, value)

        # 处理新上传的图片
        ingest_service = get_ingest_service()
        images = request.files.getlist('images')
        uploaded_images = []
        skipped_duplicates = []
        request_id = uuid.uuid4().hex

        if images:
            current_max_order = db.session.query(
                db.func.max(ProductImage.image_order)
            ).filter(ProductImage.model_number == model_number).scalar() or 0

            for idx, image_file in enumerate(images):
                if not image_file or not allowed_file(image_file.filename):
                    continue

                data = image_file.read()
                # 向量生成失败会向上抛出，不再吞掉异常静默丢数据
                result = ingest_service.ingest_one(
                    model_number, data, image_file.filename,
                    current_app.config['UPLOAD_FOLDER'],
                    image_order=current_max_order + idx + 1,
                    is_primary=False,
                    request_id=request_id,
                )
                if result.status == 'created':
                    saved_filesystem_paths.append(
                        os.path.join(
                            current_app.config['UPLOAD_FOLDER'],
                            result.image_path.removeprefix('/uploads/'),
                        )
                    )
                    uploaded_images.append(result.image_path)
                elif result.status == 'duplicate':
                    skipped_duplicates.append({
                        'filename': image_file.filename,
                        'duplicate_of': result.duplicate_of,
                    })

        db.session.commit()

        return jsonify({
            'message': '产品更新成功',
            'uploaded_images': len(uploaded_images),
            'skipped_duplicates': skipped_duplicates
        })

    except EmbeddingServiceError as e:
        db.session.rollback()
        should_cleanup_files = True
        current_app.logger.error(f"更新产品失败（向量服务）: {str(e)}")
        return error_response(str(e), 'EMBEDDING_SERVICE_ERROR', 503)
    except Exception as e:
        db.session.rollback()
        should_cleanup_files = True
        current_app.logger.error(f"更新产品失败: {str(e)}")
        return error_response(str(e), 'PRODUCT_UPDATE_FAILED', 500)
    finally:
        if should_cleanup_files:
            remove_files_quietly(saved_filesystem_paths)
```

- [ ] **Step 6: 让三个删除端点清理磁盘文件**

把 `delete_product` 中 `db.session.delete(product)` 之前加入路径收集，提交后删文件：

```python
        # 先收集磁盘路径，删行之后才能删文件
        file_paths = [
            row.original_path for row in
            ProductImage.query.filter_by(model_number=model_number).all()
        ]

        db.session.delete(product)
        db.session.commit()

        remove_files_quietly(file_paths)
```

`batch_delete_products` 中，在 `deleted_count = ...` 之前加入：

```python
        # 先收集磁盘路径；.delete() 绕过 ORM，cascade 只在数据库层生效
        file_paths = [
            row.original_path for row in
            ProductImage.query.filter(
                ProductImage.model_number.in_(model_numbers)
            ).all()
        ]
```

并在 `db.session.commit()` 之后加入 `remove_files_quietly(file_paths)`。

`delete_product_image` 中，把原来的：

```python
        # 删除物理文件
        if product_image.original_path and os.path.exists(product_image.original_path):
            os.remove(product_image.original_path)

        db.session.delete(product_image)
        db.session.commit()
```

改为（先提交再删文件，避免事务回滚后文件已丢）：

```python
        file_path = product_image.original_path

        db.session.delete(product_image)
        db.session.commit()

        remove_files_quietly([file_path])
```

- [ ] **Step 7: 运行测试确认通过**

```bash
cd backend && python -m pytest test/integration/test_write_paths.py -v
```

预期：7 passed。

- [ ] **Step 8: 确认全量回归**

```bash
cd backend && python -m pytest test/ -v
```

预期：全部通过（SQLite 测试 6 + embedding 6 + 集成 3 + 7 + 11 + 7），无 FAIL。

若 `test/test_pgvector.py`、`test/benchmark_search.py` 等旧脚本报错，它们不是 pytest 测试（是手工基准脚本），确认其失败原因与本次改动无关即可。

- [ ] **Step 9: 清理磁盘上的历史孤儿文件**

Task 1-6 完成后，`backend/uploads/product_images/cs-01/` 下那 4 个同哈希文件在数据库中已无对应行，且新代码不会再产生这类文件。手工清理：

```bash
ls -la backend/uploads/product_images/cs-01/
docker exec fashion-crm-db psql -U postgres -d image_search -c "SELECT count(*) FROM product_images;"
```

确认数据库为 0 行后再删：

```bash
rm -rf backend/uploads/product_images/cs-01/
```

- [ ] **Step 10: 提交**

```bash
git add backend/blueprints/products_v2.py backend/test/integration/test_write_paths.py
git commit -m "fix(api): update 不再吞向量异常、删除同步清理磁盘文件、重复图片明确提示"
```

---

## Task 7: CSV 导入改为批量提交

**Files:**
- Modify: `backend/blueprints/products_v2.py:459-575`（`import_csv`）
- Test: `backend/test/integration/test_import_csv.py`

**Interfaces:**
- Consumes: 无新依赖
- Produces: `POST /api/products/import-csv` 响应结构不变（`{'message', 'stats': {'total','success','failed','skipped','errors'}}`）

---

- [ ] **Step 1: 写失败的测试**

创建 `backend/test/integration/test_import_csv.py`：

```python
"""CSV 导入：批量提交、一次性存在性检查、坏行不拖垮整批。"""
import io

from models import Product, db

HEADER = 'model_number,photographer_file,alibaba_product_url,category,price_1688\n'


def _upload(client, body):
    return client.post('/api/products/import-csv', data={
        'csv_file': (io.BytesIO(body.encode('utf-8')), 'p.csv'),
    }, content_type='multipart/form-data')


def test_imports_all_valid_rows(app):
    client = app.test_client()
    rows = ''.join(
        f'CS-{i:03d},p{i},https://example.com/{i},相机肩带,{i}.50\n' for i in range(250)
    )

    response = _upload(client, HEADER + rows)

    assert response.status_code == 200
    stats = response.get_json()['stats']
    assert stats['total'] == 250
    assert stats['success'] == 250
    assert stats['failed'] == 0
    assert Product.query.count() == 250
    assert float(Product.query.get('CS-007').price_1688) == 7.50


def test_skips_existing_model_numbers(app):
    client = app.test_client()
    _upload(client, HEADER + 'CS-001,p,https://example.com/1,相机肩带,1.00\n')

    response = _upload(client, HEADER + 'CS-001,p,https://example.com/1,相机肩带,1.00\n'
                                        'CS-002,p,https://example.com/2,相机挂绳,2.00\n')

    stats = response.get_json()['stats']
    assert stats['skipped'] == 1
    assert stats['success'] == 1
    assert Product.query.count() == 2


def test_bad_row_does_not_block_good_rows(app):
    client = app.test_client()
    body = (HEADER
            + 'CS-001,p,https://example.com/1,相机肩带,1.00\n'
            + ',p,https://example.com/2,相机挂绳,2.00\n'          # 缺 model_number
            + 'CS-003,p,https://example.com/3,相机肩带,3.00\n')

    response = _upload(client, body)

    stats = response.get_json()['stats']
    assert stats['success'] == 2
    assert stats['failed'] == 1
    assert len(stats['errors']) == 1
    assert '第3行' in stats['errors'][0]
    assert {p.model_number for p in Product.query.all()} == {'CS-001', 'CS-003'}


def test_duplicate_model_number_within_same_csv_counted_once(app):
    client = app.test_client()
    body = (HEADER
            + 'CS-001,p,https://example.com/1,相机肩带,1.00\n'
            + 'CS-001,p,https://example.com/1,相机肩带,9.00\n')

    response = _upload(client, body)

    stats = response.get_json()['stats']
    assert stats['success'] == 1
    assert stats['skipped'] == 1
    assert Product.query.count() == 1


def test_gbk_encoded_csv_is_decoded(app):
    client = app.test_client()
    body = HEADER + 'CS-001,摄影师甲,https://example.com/1,相机肩带,1.00\n'

    response = client.post('/api/products/import-csv', data={
        'csv_file': (io.BytesIO(body.encode('gbk')), 'p.csv'),
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    assert Product.query.get('CS-001').photographer_file == '摄影师甲'
```

- [ ] **Step 2: 运行测试确认失败或过慢**

```bash
cd backend && python -m pytest test/integration/test_import_csv.py -v
```

预期：`test_duplicate_model_number_within_same_csv_counted_once` FAIL —— 旧实现逐行 commit，
第二行的 `Product.query.get` 能查到第一行，恰好会跳过；但若两行在同一批内则不会。
其余测试可能通过但 `test_imports_all_valid_rows` 会明显偏慢（250 个独立事务）。

记录基线耗时，供 Step 4 对比。

- [ ] **Step 3: 重写 import_csv 的行处理循环**

把 `import_csv` 中从 `# 解析 CSV` 到 `return jsonify({...})` 之前的整段替换为：

```python
        # 解析 CSV
        rows = list(csv.DictReader(io.StringIO(csv_content)))

        stats = {'total': 0, 'success': 0, 'failed': 0, 'skipped': 0, 'errors': []}

        REQUIRED_FIELDS = ['model_number', 'photographer_file', 'alibaba_product_url', 'category']
        OPTIONAL_FIELDS = [
            'spec_cn_reference', 'spec_cn', 'spec_en',
            'product_size', 'package_size',
            'price_1688', 'fob_price_tier1', 'fob_price_tier2', 'fob_price_tier3',
            'intl_platform_price', 'competitor_price',
            'ref_link_1', 'ref_link_2', 'ref_link_3',
            'intl_platform_url', 'intl_platform_url_1', 'intl_platform_url_2'
        ]
        NUMERIC_SUFFIXES = ('price_1688', 'fob_price_tier1', 'fob_price_tier2',
                            'fob_price_tier3', 'intl_platform_price', 'competitor_price')
        COMMIT_EVERY = 200

        # 一次查出全部已存在型号，替代逐行 query.get
        candidate_model_numbers = {
            (row.get('model_number') or '').strip()
            for row in rows if (row.get('model_number') or '').strip()
        }
        existing_model_numbers = {
            value for (value,) in db.session.query(Product.model_number).filter(
                Product.model_number.in_(candidate_model_numbers)
            ).all()
        } if candidate_model_numbers else set()

        pending_in_batch = 0
        for row_number, row in enumerate(rows, start=2):  # 第 1 行是表头
            stats['total'] += 1

            try:
                for field in REQUIRED_FIELDS:
                    if not row.get(field) or str(row.get(field)).strip() == '':
                        raise ValueError(f'缺少必填字段: {field}')

                model_number = row['model_number'].strip()

                # 同时覆盖「库里已有」与「同一个 CSV 内重复」
                if model_number in existing_model_numbers:
                    stats['skipped'] += 1
                    stats['errors'].append(f"第{row_number}行: 型号 {model_number} 已存在，跳过")
                    continue

                product_data = {
                    'model_number': model_number,
                    'photographer_file': row.get('photographer_file', '').strip(),
                    'alibaba_product_url': row.get('alibaba_product_url', '').strip(),
                    'category': row.get('category', '').strip(),
                }

                for field in OPTIONAL_FIELDS:
                    value = (row.get(field) or '').strip()
                    if not value:
                        continue
                    if field in NUMERIC_SUFFIXES:
                        try:
                            product_data[field] = float(value)
                        except ValueError:
                            current_app.logger.warning(f"第{row_number}行: {field} 值无效: {value}")
                    else:
                        product_data[field] = value

                db.session.add(Product.from_dict(product_data))
                existing_model_numbers.add(model_number)
                stats['success'] += 1
                pending_in_batch += 1

                if pending_in_batch >= COMMIT_EVERY:
                    db.session.commit()
                    pending_in_batch = 0

            except Exception as e:
                # 坏行不能拖垮整批：回滚后重放本批已累积的好行
                db.session.rollback()
                pending_in_batch = 0
                stats['failed'] += 1
                error_msg = f"第{row_number}行: {str(e)}"
                stats['errors'].append(error_msg)
                current_app.logger.error(error_msg)

        db.session.commit()
```

> 注意 `except` 分支里的 `db.session.rollback()` 会丢弃本批中尚未提交的好行。为避免这种情况，
> 校验失败（`ValueError`）在 `db.session.add` **之前**发生，此时 session 中没有该行；
> 而 `add` 之后到 `commit` 之间不会抛出业务异常。因此坏行只会命中前一种情况，
> rollback 时 session 里没有已 add 但未 commit 的好行——除非数据库层面报错，
> 那种情况下整批回滚是正确行为。

为让上述不变式成立，把 `stats['success'] += 1` 与 `pending_in_batch += 1` 移到
`db.session.add(...)` **之后**（上面的代码已如此），并确保所有校验都在 `add` 之前完成。

- [ ] **Step 4: 运行测试确认通过并对比耗时**

```bash
cd backend && python -m pytest test/integration/test_import_csv.py -v --durations=5
```

预期：5 passed，且 `test_imports_all_valid_rows` 的耗时明显低于 Step 2 记录的基线。

- [ ] **Step 5: 提交**

```bash
git add backend/blueprints/products_v2.py backend/test/integration/test_import_csv.py
git commit -m "perf(api): CSV 导入改为批量提交与一次性存在性检查"
```

---

## Task 8: 目录批量导入 CLI

**Files:**
- Create: `backend/scripts/ingest_images.py`
- Delete: `backend/scripts/ingest_dataset.py`
- Test: `backend/test/integration/test_ingest_cli.py`

**Interfaces:**
- Consumes: `services.ingest.{ImageIngestService, PendingImage, hash_file, find_existing_hashes, ALLOWED_EXTENSIONS}`（T4）、`services.embedding.MAX_BATCH_SIZE`（T2）
- Produces:
  - `scan_directory(root: str) -> dict[str, list[str]]`（`{model_number: [排序后的图片绝对路径]}`）
  - `build_plan(scanned, known_model_numbers, existing_hashes) -> IngestPlan`
  - `IngestPlan`（dataclass：`pending: list[PendingImage]`、`duplicates: list[tuple[str, str]]`、`orphan_dirs: list[str]`）
  - `run(app, root, dry_run, rebuild_index, batch_size, limit) -> IngestReport`
  - `IngestReport`（dataclass：`created`、`duplicates`、`orphan_dirs`、`failed`、`elapsed_seconds`）

---

- [ ] **Step 1: 写失败的测试**

创建 `backend/test/integration/test_ingest_cli.py`：

```python
"""目录批量导入 CLI：孤儿目录报告、dry-run、幂等。"""
import io
import os

import numpy as np
from PIL import Image

from models import Product, ProductImage, db


def _write_png(path, color):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new('RGB', (8, 8), color).save(path, format='PNG')


class CountingEmbedding:
    def __init__(self):
        self.image_calls = 0

    def embed_image(self, image_path, request_id=None):
        self.image_calls += 1
        return np.full(1024, 0.1, dtype=np.float32)

    def embed_images(self, image_paths, request_id=None):
        self.image_calls += len(image_paths)
        return [np.full(1024, 0.1, dtype=np.float32) for _ in image_paths]


def _add_product(model_number):
    db.session.add(Product(
        model_number=model_number, photographer_file='p',
        alibaba_product_url='https://example.com/x', category='相机肩带',
    ))
    db.session.commit()


def test_scan_directory_maps_dirname_to_model_number(tmp_path):
    from scripts.ingest_images import scan_directory

    _write_png(str(tmp_path / 'CS-001' / '1.png'), 'red')
    _write_png(str(tmp_path / 'CS-001' / '2.png'), 'blue')
    _write_png(str(tmp_path / 'HL-002' / '主图.PNG'), 'green')
    (tmp_path / 'CS-001' / 'notes.txt').write_text('忽略我')
    _write_png(str(tmp_path / '散图.png'), 'black')  # root 下散图不属于任何型号

    scanned = scan_directory(str(tmp_path))

    assert set(scanned) == {'CS-001', 'HL-002'}
    assert [os.path.basename(p) for p in scanned['CS-001']] == ['1.png', '2.png']
    assert [os.path.basename(p) for p in scanned['HL-002']] == ['主图.PNG']


def test_scan_directory_recurses_inside_model_directory(tmp_path):
    from scripts.ingest_images import scan_directory

    _write_png(str(tmp_path / 'CS-001' / '细节图' / 'a.png'), 'red')

    scanned = scan_directory(str(tmp_path))

    assert len(scanned['CS-001']) == 1
    assert scanned['CS-001'][0].endswith(os.path.join('细节图', 'a.png'))


def test_orphan_directories_are_reported_and_skipped(app, tmp_path):
    from scripts.ingest_images import run

    _add_product('CS-001')
    _write_png(str(tmp_path / 'CS-001' / '1.png'), 'red')
    _write_png(str(tmp_path / 'CS-007' / '1.png'), 'blue')
    _write_png(str(tmp_path / 'CS-08' / '1.png'), 'green')

    report = run(app, str(tmp_path), embedding_client=CountingEmbedding())

    assert sorted(report.orphan_dirs) == ['CS-007', 'CS-08']
    assert report.created == 1
    assert ProductImage.query.count() == 1


def test_dry_run_writes_nothing_and_calls_no_api(app, tmp_path):
    from scripts.ingest_images import run

    _add_product('CS-001')
    _write_png(str(tmp_path / 'CS-001' / '1.png'), 'red')
    embedding = CountingEmbedding()

    report = run(app, str(tmp_path), dry_run=True, embedding_client=embedding)

    assert report.created == 1          # 报告「将会入库 1 张」
    assert ProductImage.query.count() == 0
    assert embedding.image_calls == 0


def test_rerun_is_idempotent_with_zero_api_calls(app, tmp_path):
    from scripts.ingest_images import run

    _add_product('CS-001')
    _write_png(str(tmp_path / 'CS-001' / '1.png'), 'red')
    _write_png(str(tmp_path / 'CS-001' / '2.png'), 'blue')

    first_embedding = CountingEmbedding()
    first = run(app, str(tmp_path), embedding_client=first_embedding)
    assert first.created == 2
    assert first_embedding.image_calls == 2

    second_embedding = CountingEmbedding()
    second = run(app, str(tmp_path), embedding_client=second_embedding)

    assert second.created == 0
    assert second.duplicates == 2
    assert second_embedding.image_calls == 0
    assert ProductImage.query.count() == 2


def test_duplicate_across_model_directories_is_reported(app, tmp_path):
    from scripts.ingest_images import run

    _add_product('CS-001')
    _add_product('HL-002')
    _write_png(str(tmp_path / 'CS-001' / '1.png'), 'red')
    # 完全相同的内容放到另一个型号目录下
    _write_png(str(tmp_path / 'HL-002' / '主图.png'), 'red')

    report = run(app, str(tmp_path), embedding_client=CountingEmbedding())

    assert report.created == 1
    assert report.duplicates == 1
    assert ProductImage.query.count() == 1


def test_first_image_of_each_product_is_primary(app, tmp_path):
    from scripts.ingest_images import run

    _add_product('CS-001')
    _write_png(str(tmp_path / 'CS-001' / '1.png'), 'red')
    _write_png(str(tmp_path / 'CS-001' / '2.png'), 'blue')

    run(app, str(tmp_path), embedding_client=CountingEmbedding())

    primaries = ProductImage.query.filter_by(is_primary=True).all()
    assert len(primaries) == 1
    assert primaries[0].image_order == 0


def test_limit_caps_processed_images(app, tmp_path):
    from scripts.ingest_images import run

    _add_product('CS-001')
    for i, color in enumerate(('red', 'blue', 'green')):
        _write_png(str(tmp_path / 'CS-001' / f'{i}.png'), color)

    report = run(app, str(tmp_path), limit=2, embedding_client=CountingEmbedding())

    assert report.created == 2
    assert ProductImage.query.count() == 2
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python -m pytest test/integration/test_ingest_cli.py -v
```

预期：collection error，`ModuleNotFoundError: No module named 'scripts.ingest_images'`。

- [ ] **Step 3: 删除废弃脚本**

```bash
git rm backend/scripts/ingest_dataset.py
```

该脚本顶部即 `raise SystemExit`，且引用了已不存在的 `VectorProductIndex`。

- [ ] **Step 4: 实现 CLI**

创建 `backend/scripts/ingest_images.py`：

```python
#!/usr/bin/env python3
"""目录批量导入图片与向量。

约定：`--root` 的一级子目录名即 model_number，型号目录内部递归收图。
先跑 CSV 建产品，再跑本脚本导图；目录名对不上已有型号的一律跳过并报告。

用法：
    python -m scripts.ingest_images --root data/摄像师拍摄素材 --dry-run
    python -m scripts.ingest_images --root data/摄像师拍摄素材 --rebuild-index
"""
import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

from app import create_app  # noqa: E402
from models import Product, db  # noqa: E402
from services.embedding import MAX_BATCH_SIZE, EmbeddingClient  # noqa: E402
from services.ingest import (  # noqa: E402
    ALLOWED_EXTENSIONS,
    ImageIngestService,
    PendingImage,
    find_existing_hashes,
    hash_file,
)

logger = logging.getLogger('ingest_images')

# 每张图约 402 tokens，0.0005 元/千 token
YUAN_PER_IMAGE = 402 * 0.0005 / 1000

_HNSW_INDEX = 'idx_product_images_vector_hnsw'


@dataclass
class IngestPlan:
    pending: list = field(default_factory=list)          # list[PendingImage]
    duplicates: list = field(default_factory=list)       # list[(源路径, 已存在的 image_path)]
    orphan_dirs: list = field(default_factory=list)      # list[model_number]


@dataclass
class IngestReport:
    created: int = 0
    duplicates: int = 0
    failed: int = 0
    orphan_dirs: list = field(default_factory=list)
    duplicate_details: list = field(default_factory=list)
    failed_details: list = field(default_factory=list)
    scanned: int = 0
    elapsed_seconds: float = 0.0


def scan_directory(root):
    """{model_number: [排序后的图片绝对路径]}。

    一级子目录名 = model_number；型号目录内部递归收图。
    root 下直接存放的散图不属于任何型号，会被忽略（由调用方计入孤儿）。
    """
    scanned = {}
    root_path = Path(root)
    for entry in sorted(root_path.iterdir()):
        if not entry.is_dir():
            continue
        images = sorted(
            str(p.resolve()) for p in entry.rglob('*')
            if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS
        )
        if images:
            scanned[entry.name] = images
    return scanned


def build_plan(scanned, known_model_numbers, existing_hashes, limit=None):
    """把扫描结果切成「待入库 / 已重复 / 孤儿目录」三堆。

    existing_hashes: {content_hash: 已存在的 image_path}
    """
    plan = IngestPlan()
    seen = dict(existing_hashes)
    processed = 0

    for model_number in sorted(scanned):
        if model_number not in known_model_numbers:
            plan.orphan_dirs.append(model_number)
            continue

        for order, source_path in enumerate(scanned[model_number]):
            if limit is not None and processed >= limit:
                return plan
            processed += 1

            content_hash = hash_file(source_path)
            if content_hash in seen:
                plan.duplicates.append((source_path, seen[content_hash]))
                continue

            plan.pending.append(PendingImage(
                model_number=model_number,
                source_path=source_path,
                content_hash=content_hash,
                image_order=order,
                is_primary=(order == 0),
            ))
            seen[content_hash] = source_path

    return plan


def _drop_hnsw_index():
    db.session.execute(text(f'DROP INDEX IF EXISTS {_HNSW_INDEX}'))
    db.session.commit()
    logger.info('已删除 HNSW 索引，导入结束后重建')


def _create_hnsw_index():
    # 建索引期间临时放宽内存与并行度（pgvector 官方建议）
    db.session.execute(text("SET maintenance_work_mem = '2GB'"))
    db.session.execute(text('SET max_parallel_maintenance_workers = 7'))
    db.session.execute(text(
        f'CREATE INDEX IF NOT EXISTS {_HNSW_INDEX} '
        'ON product_images USING hnsw (vector vector_cosine_ops) '
        'WITH (m = 16, ef_construction = 64)'
    ))
    db.session.execute(text('RESET maintenance_work_mem'))
    db.session.execute(text('RESET max_parallel_maintenance_workers'))
    db.session.execute(text('ANALYZE product_images'))
    db.session.commit()
    logger.info('HNSW 索引已重建')


def run(app, root, dry_run=False, rebuild_index=False, batch_size=MAX_BATCH_SIZE,
        limit=None, embedding_client=None):
    """执行导入，返回 IngestReport。app 需已进入 app_context 或本函数自行进入。"""
    started = time.perf_counter()

    def _execute():
        scanned = scan_directory(root)
        total_scanned = sum(len(v) for v in scanned.values())

        known = {
            value for (value,) in db.session.query(Product.model_number).all()
        }

        # 先批量算哈希，再一次性查库，避免逐张查询
        all_hashes = []
        for model_number, paths in scanned.items():
            if model_number in known:
                all_hashes.extend(hash_file(p) for p in paths)
        existing = find_existing_hashes(all_hashes)

        plan = build_plan(scanned, known, existing, limit=limit)

        report = IngestReport(
            duplicates=len(plan.duplicates),
            orphan_dirs=plan.orphan_dirs,
            duplicate_details=plan.duplicates,
            scanned=total_scanned,
        )

        if dry_run:
            report.created = len(plan.pending)
            return report

        service = ImageIngestService(embedding_client=embedding_client or EmbeddingClient())
        upload_folder = app.config['UPLOAD_FOLDER']
        effective_batch = max(1, min(int(batch_size), MAX_BATCH_SIZE))

        for start in range(0, len(plan.pending), effective_batch):
            chunk = plan.pending[start:start + effective_batch]
            try:
                results = service.ingest_pending(chunk, upload_folder)
                db.session.commit()
            except Exception as exc:  # noqa: BLE001 - 单批失败不应终止整次导入
                db.session.rollback()
                logger.error('批次写入失败 start=%s size=%s error=%s', start, len(chunk), exc)
                report.failed += len(chunk)
                report.failed_details.extend((item.source_path, str(exc)) for item in chunk)
                continue

            for result in results:
                if result.status == 'created':
                    report.created += 1
                elif result.status == 'duplicate':
                    report.duplicates += 1
                    report.duplicate_details.append((result.source_path, result.duplicate_of))
                else:
                    report.failed += 1
                    report.failed_details.append((result.source_path, result.error))

            logger.info('进度 %s/%s', min(start + effective_batch, len(plan.pending)),
                        len(plan.pending))

        return report

    if rebuild_index and not dry_run:
        with app.app_context():
            _drop_hnsw_index()

    # 测试传入的 app fixture 已处于 app_context 内；Flask 支持嵌套，无冲突
    with app.app_context():
        report = _execute()

    if rebuild_index and not dry_run:
        with app.app_context():
            _create_hnsw_index()

    report.elapsed_seconds = time.perf_counter() - started
    return report


def print_report(report, dry_run):
    prefix = '[DRY-RUN] ' if dry_run else ''
    print(f'\n{prefix}扫描: {report.scanned} 张')
    print(f'  ✓ {"将入库" if dry_run else "入库"}      {report.created} 张')
    print(f'  ⊘ 重复跳过    {report.duplicates} 张（节省 ¥{report.duplicates * YUAN_PER_IMAGE:.3f}）')
    for source, existing in report.duplicate_details[:20]:
        print(f'      {source}  与 {existing} 内容相同')
    if len(report.duplicate_details) > 20:
        print(f'      …… 其余 {len(report.duplicate_details) - 20} 条见日志')
    print(f'  ✗ 孤儿目录    {len(report.orphan_dirs)} 个（无对应产品，已跳过）')
    if report.orphan_dirs:
        print(f'      {", ".join(report.orphan_dirs)}')
    print(f'  ✗ 失败        {report.failed} 张')
    for source, error in report.failed_details[:20]:
        print(f'      {source}: {error}')
    print(f'\n耗时 {report.elapsed_seconds:.1f}s / API 费用约 ¥{report.created * YUAN_PER_IMAGE:.2f}\n')


def create_parser():
    parser = argparse.ArgumentParser(description='批量导入本地目录中的产品图片与向量。')
    parser.add_argument('--root', help='素材根目录，默认取 Flask 配置 DATASET_ROOT')
    parser.add_argument('--dry-run', action='store_true',
                        help='只扫描算哈希查重并报告，不调 API、不写库、不落盘')
    parser.add_argument('--rebuild-index', action='store_true',
                        help='导入前删除 HNSW 索引，导入后重建（首次全量导入用）')
    parser.add_argument('--batch-size', type=int, default=MAX_BATCH_SIZE,
                        help=f'每次 DashScope 请求的图片数，clamp 到 [1, {MAX_BATCH_SIZE}]')
    parser.add_argument('--limit', type=int, help='只处理前 N 张，用于调试')
    return parser


def main():
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
    args = create_parser().parse_args()

    app = create_app()
    root = args.root or app.config.get('DATASET_ROOT', '')
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        raise SystemExit(f'素材目录不存在: {root}')

    logger.info('素材目录: %s', root)
    report = run(
        app, root,
        dry_run=args.dry_run,
        rebuild_index=args.rebuild_index,
        batch_size=args.batch_size,
        limit=args.limit,
    )
    print_report(report, args.dry_run)


if __name__ == '__main__':
    main()
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && python -m pytest test/integration/test_ingest_cli.py -v
```

预期：8 passed。

若 `test_scan_directory_maps_dirname_to_model_number` 报 `ModuleNotFoundError: No module named 'scripts'`，
在 `backend/scripts/` 下创建空文件 `__init__.py` 并重试。

- [ ] **Step 6: 用真实数据做一次 dry-run 冒烟**

```bash
mkdir -p /tmp/ingest_smoke/CS-001 /tmp/ingest_smoke/UNKNOWN-999
cp backend/uploads/product_images/cs-01/*.png /tmp/ingest_smoke/CS-001/ 2>/dev/null || \
  python -c "from PIL import Image; Image.new('RGB',(64,64),'red').save('/tmp/ingest_smoke/CS-001/1.png')"
python -c "from PIL import Image; Image.new('RGB',(64,64),'blue').save('/tmp/ingest_smoke/UNKNOWN-999/1.png')"

cd backend && python -m scripts.ingest_images --root /tmp/ingest_smoke --dry-run
```

预期输出中包含 `孤儿目录    1 个` 且列出 `UNKNOWN-999`；`CS-001` 若不在库中也会被列为孤儿。
`--dry-run` 不得产生任何 API 调用（观察是否有 `embedding.success` 日志）。

清理：`rm -rf /tmp/ingest_smoke`

- [ ] **Step 7: 提交**

```bash
git add backend/scripts/ingest_images.py backend/test/integration/test_ingest_cli.py
git rm --cached backend/scripts/ingest_dataset.py 2>/dev/null || true
git commit -m "feat(cli): 新增目录批量导入脚本，幂等去重、批量 embedding、可选重建索引"
```

---

## Task 9: 文档同步

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: T1-T8 的全部产出
- Produces: 无代码接口

---

- [ ] **Step 1: 更新 Schema 章节**

在 `CLAUDE.md` 的 `### product_images` 表格中，`vector` 行之后加入一行：

```markdown
| content_hash | VARCHAR(64) | UNIQUE（全库），源文件 SHA-256，用于精确去重 |
```

并把 **Indexes** 那行改为：

```markdown
**Indexes**: `idx_product_images_model_number`, `uq_product_images_content_hash` (UNIQUE), `idx_product_images_vector_hnsw` (HNSW, `vector_cosine_ops`, m=16, ef_construction=64)
```

- [ ] **Step 2: 更新架构章节**

把 **Critical Files** 列表中的 `[backend/product_search.py](backend/product_search.py) - ImageSearchService: stateless embedding + vector search` 替换为：

```markdown
- [backend/services/embedding.py](backend/services/embedding.py) - `EmbeddingClient`: DashScope 调用，单张 + 批量（**硬上限 20 张/请求**）、429 重试、批失败降级为单张
- [backend/services/vector_search.py](backend/services/vector_search.py) - `VectorSearchService`: pgvector 检索，SQL 内过采样 + `DISTINCT ON` 按型号折叠
- [backend/services/ingest.py](backend/services/ingest.py) - `ImageIngestService`: SHA-256 全库去重 + 哈希命名落盘 + 入库，CLI 与 API 共用
- [backend/product_search.py](backend/product_search.py) - 兼容层，仅再导出 `services/` 中的符号
- [backend/scripts/ingest_images.py](backend/scripts/ingest_images.py) - 目录批量导入 CLI
```

- [ ] **Step 3: 重写 Vector Search Implementation 章节的 SQL**

把 **SQL Query Pattern** 代码块替换为：

````markdown
**SQL Query Pattern**（cosine；先过采样再按型号折叠，不是取 top_k 张图后在 Python 里折叠）:
```sql
SET LOCAL hnsw.ef_search = :ef;   -- ef = max(fetch_n, 40)；LOCAL 避免污染连接池
WITH candidates AS MATERIALIZED (
    SELECT model_number, image_path, original_path, oss_path,
           vector <=> CAST(:q AS vector) AS distance
    FROM product_images ORDER BY vector <=> CAST(:q AS vector) LIMIT :fetch_n
), best AS (
    SELECT DISTINCT ON (model_number) * FROM candidates ORDER BY model_number, distance
)
SELECT * FROM best ORDER BY distance LIMIT :top_k;
```
- `fetch_n = clamp(top_k × SEARCH_OVERSAMPLE, top_k, 500)`，`SEARCH_OVERSAMPLE` 默认 5
- `similarity = min(1.0, max(0.0, 1.0 - distance))` —— 实测向量 L2 范数 1.000282，必须夹上界
- ⚠️ pgvector **不会**把 `ef_search` clamp 到 k（见 `src/hnswscan.c`），所以必须显式设置
````

- [ ] **Step 4: 新增批量导入章节**

在 `## CSV Import` 章节之后插入：

````markdown
## 批量导入图片（CLI）

目录约定：`{root}/{model_number}/**/*.{jpg,png,…}` —— 一级子目录名即型号，目录内部递归收图。

```bash
cd backend
python -m scripts.ingest_images --root data/摄像师拍摄素材 --dry-run    # 先看报告
python -m scripts.ingest_images --root data/摄像师拍摄素材 --rebuild-index  # 首次全量
python -m scripts.ingest_images --root data/摄像师拍摄素材              # 日常增量
```

- **顺序**：先 `POST /api/products/import-csv` 建产品，再跑本脚本导图。目录名匹配不上已有 `model_number` 的会被跳过并在报告中列出（常见于 `CS-08` 应为 `CS-008` 这类命名错误）。
- **去重**：SHA-256 全库唯一。重复图片在调用 DashScope **之前**就被拦下，不花钱。
- **幂等**：重跑同一目录零 API 调用，因此断点续传无需 checkpoint。
- **性能**：实测批量 20 张/请求 = 89 ms/张（单张为 490 ms/张，约 5.5× 提升）。1000 张约 90 秒、¥0.2。
- `--rebuild-index` 会先 `DROP` 再重建 HNSW 索引（pgvector 官方建议先灌数据后建索引），仅首次全量导入使用。
````

- [ ] **Step 5: 更新 Testing 章节**

把 `## Testing` 的代码块替换为：

````markdown
```bash
cd backend
python -m pytest test/ -v                        # 全部（集成测试需要 docker compose up -d db）
python -m pytest test/integration/ -v            # 仅集成测试（真 PostgreSQL）
python -m pytest test/ --ignore=test/integration -v   # 仅单元测试（无需数据库）
python -m pytest test/ --cov=. --cov-report=html
```

集成测试连接 `DB_HOST:DB_PORT`（默认 `localhost:5433`）上的独立库 `image_search_test`，每个测试
重建表结构，不污染开发数据。PostgreSQL 不可达时整套集成测试自动 skip。

⚠️ `test/test_pgvector.py`、`test/benchmark_search.py` 是手工基准脚本，不是 pytest 用例。
````

- [ ] **Step 6: 更新 Important Architecture Notes**

把「**Deprecated scripts**」那条替换为：

```markdown
- **批量导入**: 用 `python -m scripts.ingest_images`（见上方章节）。FAISS 时代的 `ingest_dataset.py` 已删除。
```

在该列表末尾追加两条：

```markdown
- **图片去重**: `product_images.content_hash` 为源文件 SHA-256，**全库唯一**。同一张图在任何型号下只能入库一次；重复上传返回 `{skipped: true, duplicate_of: ...}`。近似去重（pHash）暂未实现。
- **图片文件命名**: `uploads/product_images/{model_number}/{sha256前16位}{ext}`，不再用 UUID。同一张图永远落在同一路径，重复导入不产生新文件。
```

- [ ] **Step 7: 更新环境变量章节**

在 `**Optional**:` 列表中加入：

```markdown
- `SEARCH_OVERSAMPLE` - 检索过采样系数，默认 5（≈ 单型号平均图片数）。产品图片数普遍偏多时调大
```

- [ ] **Step 8: 全量回归 + 提交**

```bash
cd backend && python -m pytest test/ -v
```

预期：全部通过。

```bash
git add CLAUDE.md
git commit -m "docs: 同步重构后的架构、schema、批量导入与测试说明"
```

---

## 自检记录

**Spec 覆盖检查**

| Spec 章节 | 对应任务 |
|---|---|
| §3.1 模块拆分 | T2（embedding）、T3（vector_search + 兼容层）、T4（ingest） |
| §4.1 / §4.2 Schema 三处同步 | T1 |
| §4.3 哈希命名 | T4（`storage_paths`） |
| §5.1-5.3 检索 SQL / 参数 / SET LOCAL / 相似度夹紧 / 去 JOIN | T3 |
| §5.4 已知局限 | T9（文档如实说明） |
| §6.1-6.7 CLI | T8 |
| §7.1 update 不吞异常 | T6 |
| §7.2 删除清理文件 | T6 |
| §7.3 重复图片明确提示 | T6 |
| §7.4 429 判断 + 重试合并 | T2 |
| §7.5 CSV 批量提交 | T7 |
| §8 错误处理 | T2（重试）、T6（HTTP 映射） |
| §9 测试策略 | T1（基建）、T3、T4、T6、T7、T8 |
| §10 YAGNI | 全篇未实现，T9 在文档中标注 |
| §11 交付清单 | T1-T9 全覆盖 |

无遗漏。

**命名一致性检查**

- `content_hash` / `uq_product_images_content_hash` / `idx_product_images_vector_hnsw` 在 T1、T4、T8、T9 中拼写一致
- `IngestResult.status` 取值 `'created' | 'duplicate' | 'failed'` 在 T4 定义、T4/T6/T8 消费，一致
- `storage_paths` 返回 `(web_path, filesystem_path)` 在 T4 定义、T4/T6 消费，顺序一致
- `EmbeddingClient.embed_images` 返回 `list[np.ndarray | None]` 在 T2 定义、T4 消费（`if vector is None`），一致
- `VectorSearchService.search_similar_images` 签名与旧 `ImageSearchService` 完全一致，T5 无需改调用方
- `find_existing_hashes` 在 T4 定义、T8 消费，返回 `{hash: image_path}`，一致

**自检中修掉的问题**

- T8 原有一个「先写错再改对」的步骤（`run()` 里冗余的 app_context 判断），已直接在 Step 4 写成正确版本并删掉该步骤。
- T8 `print_report` 里一处无意义的空表达式已修正。
- Spec §4.1 原写 `CHAR(64)`，与 SQLAlchemy `db.String(64)` 对不齐（PostgreSQL 的 `char(n)` 会空格填充）。已把 spec 与本计划统一为 `VARCHAR(64)`。
