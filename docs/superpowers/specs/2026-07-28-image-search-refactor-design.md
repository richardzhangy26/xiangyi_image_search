# 以图搜款重构 + 批量导入 + 图片去重 设计文档

- 日期：2026-07-28
- 状态：已评审通过，待拆解实施计划
- 影响范围：`backend/product_search.py`、`backend/blueprints/products_v2.py`、`backend/models/product.py`、`postgres/init/01_init.sql`、`backend/init_db.py`、`backend/scripts/`、`backend/test/`

---

## 1. 背景与动机

现有以图搜款功能可以跑通，但存在功能性缺陷、无去重机制、无批量导入入口。本次重构在不改变「本地 PostgreSQL + pgvector + DashScope 通义多模态向量」这一技术选型的前提下，修复检索语义、补齐批量导入与去重能力。

### 1.1 实测基线（2026-07-28 测得，非推测）

| 项 | 实测结果 |
|---|---|
| 数据库现状 | `products` 0 行、`product_images` 0 行 |
| pgvector 版本 | 0.8.5 |
| 磁盘现状 | `backend/uploads/product_images/cs-01/` 存在 4 个 SHA-256 完全相同的文件，各带独立 UUID 前缀 |
| DashScope 批量上限 | 一次请求最多 20 个内容元素；传 32 个返回 `400 contents count (32) exceeds limit (20)` |
| 批量吞吐收益 | 单张 490 ms/张 → 20 张/请求 89 ms/张，约 **5.5×**；token 计费不变（402 tokens/图） |
| 向量确定性 | 同一张图两次调用返回的向量**逐位相同** |
| 向量归一化 | L2 范数 1.000282（非精确归一化）；同图余弦相似度 1.00056 |
| `hnsw.ef_search` | 全项目仅出现在 `backend/test/test_pgvector.py:143`，检索路径从未设置，运行时取默认值 40 |
| 现有测试 | `test_products_v2_search_behaviors.py` 5 passed，但用 SQLite in-memory + `FakeSearchService`，**向量 SQL 零覆盖** |

磁盘上那 4 个同哈希文件 + 空数据库，同时印证了两个缺陷：同一张图被重复导入 4 次（4 次 API 调用、4 条向量、4 行记录），以及删除产品时未清理磁盘文件。

### 1.2 已确认的缺陷清单

**阻断级**

1. `validate_top_k` 允许 `top_k` 最大 50，但 `hnsw.ef_search` 为默认值 40。经核对 pgvector 源码 `src/hnswscan.c`，`GetScanItems` 将 `hnsw_ef_search` 直接传入 `HnswSearchLayer`，**不会** clamp 到 k。`top_k > 40` 时召回率显著下降。
2. 去重发生在 SQL 之后。`search_similar_images` 取 top_k 条**图片**，再由 `dedupe_results_by_model_number` 折叠成**产品**。一个产品有 5 张图时，`top_k=10` 可能仅返回 2 个产品。用户语义是「返回 N 个相似款」，实际行为是「返回 N 张相似图折叠后的剩余数量」。这是功能缺陷，非性能问题。
3. `update_product`（`blueprints/products_v2.py:312-313`）捕获向量生成异常后仅记录日志：图片文件已落盘、数据库无记录、接口返回 200「更新成功」。静默数据丢失。且缺少 `create_product` 已有的失败文件清理逻辑。
4. 完全没有去重机制。`image_path` 的 UNIQUE 约束形同虚设，因为 `save_product_image` 每次拼接新的 `uuid4()` 前缀，永不冲突。

**功能级**

5. `delete_product` 与 `batch_delete_products` 只删数据库行，不删磁盘文件。`batch_delete_products` 使用 `.delete(synchronize_session=False)` 绕过 ORM。
6. 相似度可能大于 1。`max(0.0, 1.0 - distance)` 只夹下界；实测同图会显示 100.1%。
7. 无批量导入入口。`backend/scripts/ingest_dataset.py` 顶部即 `raise SystemExit` 废弃，且引用了已不存在的 `VectorProductIndex`。
8. CSV 导入逐行 commit，N 行 = N 个事务 + N 次 `Product.query.get`。
9. 限流判断依赖字符串匹配 `"rate limit exceeded"`；且重试逻辑在 `status_code` 分支与 `except` 分支各写了一遍。

**架构级**

10. `postgres/init/01_init.sql` 在空表上即创建 HNSW 索引。pgvector 官方建议先灌数据后建索引，否则批量导入时每插一行都要维护图结构。
11. 检索 SQL 中 `join(Product)` 冗余（FK + CASCADE 已保证无孤儿行），徒增查询计划选错的风险。
12. `maintenance_work_mem`、`max_parallel_maintenance_workers` 均为默认值；pgvector 0.8.5 已支持的 `iterative_scan` 未启用。

### 1.3 调研结论

**pgvector 最佳实践**（来源：[pgvector README](https://github.com/pgvector/pgvector/blob/master/README.md)、[pgvector DBA 指南 2026-03](https://www.dbi-services.com/blog/pgvector-a-guide-for-dba-part-2-indexes-update-march-2026/)、[Clarvo 过滤查询优化](https://www.clarvo.ai/blog/optimizing-filtered-vector-queries-from-tens-of-seconds-to-single-digit-milliseconds-in-postgresql)）

- 先加载数据、后创建索引；生产环境用 `CREATE INDEX CONCURRENTLY`
- 批量加载优先使用 `COPY`（二进制格式）
- `hnsw.ef_search`（默认 40）必须 ≥ 实际需要的候选数
- 建索引时提高 `maintenance_work_mem`（图放不下内存会有 notice）与 `max_parallel_maintenance_workers`（默认 2）
- 带过滤的查询使用 `hnsw.iterative_scan`（0.8.0+，`strict_order` / `relaxed_order`）或 partial index
- HNSW 图需常驻 `shared_buffers` / OS cache

**图片去重最佳实践**（来源：[感知哈希 vs 深度嵌入对比研究](https://www.mdpi.com/2079-9292/15/7/1493)、[imagededup](https://idealo.github.io/imagededup/)）

业界标准为两层：先用 SHA-256/MD5 做精确去重（在调用 API 之前拦截，直接节省费用），剩余部分再用 pHash 做近似去重（尺寸变化、水印、重压缩）。纯感知哈希对几何变换鲁棒性差，纯 CNN 嵌入计算成本高。

本项目实测向量具有确定性（同图逐位相同），因此精确层用内容哈希与用向量比对在效果上等价，但哈希无需调用 API，成本为零。

---

## 2. 设计决策（已确认）

| 决策点 | 选择 | 理由 |
|---|---|---|
| 数据源形态 | 目录名即型号：`{root}/{model_number}/{任意文件名}` | 与 `app.py:57` 中 `DATASET_ROOT` 默认值 `backend/data/摄像师拍摄素材` 一致 |
| 去重边界 | **全库唯一**：`content_hash` 全库 UNIQUE | 两个型号共用同一张图基本意味着素材归档出错，应当被发现并报告 |
| 去重层次 | **仅 SHA-256 精确去重**，不做 pHash | KISS；零依赖、零误拦、在调用 API 前拦截。磁盘上现存的 4 个重复文件正属此类 |
| 孤儿型号目录 | **跳过并报告** | 先 CSV 建产品，再扫目录导图。保持 `products` 数据质量，并能反向发现目录命名错误（如 `CS-08` 应为 `CS-008`） |
| 批量导入形态 | **仅 CLI 脚本** | 数千张图走浏览器上传不现实；前端保持现有单产品上传流程 |

---

## 3. 架构设计

### 3.1 模块拆分

当前 `backend/product_search.py` 单文件混合了四种职责：图片压缩、DashScope 调用、重试策略、向量检索。CLI 需要复用 embedding 但不需要检索，API 需要复用去重但不需要扫盘。按职责拆为三个模块：

```
backend/services/
  __init__.py
  embedding.py      # EmbeddingClient
  vector_search.py  # VectorSearchService
  ingest.py         # ImageIngestService
```

| 模块 | 职责 | 对外接口 |
|---|---|---|
| `embedding.py` | 图片读取/压缩/base64、DashScope 单张与批量调用、重试与限流处理 | `embed_image(path) -> np.ndarray`<br>`embed_images(paths: list[str]) -> list[np.ndarray \| None]`<br>`embed_text(text) -> np.ndarray` |
| `vector_search.py` | pgvector 检索、`ef_search` 设置、结果格式化 | `search_by_vector(vector, top_k) -> list[dict]`<br>`search_by_image(path, top_k) -> list[dict]` |
| `ingest.py` | 内容哈希、查重、落盘、入库。CLI 与 API 共用 | `hash_bytes(data) -> str`<br>`find_duplicates(hashes) -> dict[str, str]`<br>`ingest_batch(items) -> IngestReport`<br>`ingest_one(model_number, data, filename) -> IngestResult` |

`backend/product_search.py` 保留为薄兼容层：

```python
from services.embedding import EmbeddingServiceError, EMBEDDING_MODEL, EMBEDDING_DIMENSION
from services.vector_search import VectorSearchError, VectorSearchService

ImageSearchService = VectorSearchService  # 兼容 app.py 与现有测试
```

这样 `app.py:10`、`app.py:72` 与 `blueprints/products_v2.py:14` 的导入无需修改，现有 5 个测试也不用动。

**依赖方向**：`ingest.py` → `embedding.py`；`vector_search.py` → `embedding.py`；三者均依赖 `models`。无循环依赖。

### 3.2 数据流

**检索**

```
上传图片 → 内存字节 → EmbeddingClient.embed_image → 1024 维向量
  → VectorSearchService：SET LOCAL ef_search → 过采样 → DISTINCT ON 折叠 → top_k 产品
  → 批量查 Product 详情 → JSON
```

**CLI 批量导入**

```
扫目录 → {model_number: [paths]}
  → 查已有 model_number（1 次查询）→ 分出孤儿目录
  → 本地算全部 SHA-256（零 API 成本）
  → 查已有 content_hash（1 次查询）→ 过滤重复 + 批内去重
  → 分批 ≤20 张 → EmbeddingClient.embed_images
  → 每批一个事务：落盘 + 写库
  → 汇总报告
```

---

## 4. Schema 变更

### 4.1 变更内容

```sql
ALTER TABLE product_images ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64);
CREATE UNIQUE INDEX IF NOT EXISTS uq_product_images_content_hash
    ON product_images (content_hash);
```

- `content_hash`：源文件原始字节的 SHA-256 十六进制小写，64 字符。类型统一用 `VARCHAR(64)`（不用 `CHAR(64)`：PostgreSQL 的 `char(n)` 会做空格填充，且与 SQLAlchemy `db.String(64)` 对不齐）。
  - 「原始字节」指磁盘/上传流中的原始文件内容，**不是**压缩转 JPEG 之后的字节。这样同一源文件无论压缩参数如何变化，哈希都稳定。
- 列定义为 nullable（当前 0 行数据，无需回填；UNIQUE 索引在 PostgreSQL 中允许多个 NULL）。新写入路径一律填值。

### 4.2 三处同步

Schema 在项目中定义于三处，必须同步修改：

1. `backend/models/product.py` — SQLAlchemy 模型（权威定义），`ProductImage` 增加 `content_hash` 列并加入 `to_dict()`
2. `postgres/init/01_init.sql` — Docker 首次启动初始化，加入列定义与唯一索引
3. `backend/init_db.py` — 增加上述 `ALTER TABLE` / `CREATE UNIQUE INDEX` 语句，使已存在的库也能收敛到新结构

当前数据库为空，因此**不需要 `docker compose down -v`**，也不需要数据回填。执行 `python init_db.py` 即可完成迁移。

### 4.3 文件命名规则变更

从 `{uuid4()}_{secure_filename(原名)}` 改为：

```
uploads/product_images/{model_number}/{content_hash[:16]}{ext}
```

- `ext` 取自原文件名后缀，小写归一（`.JPG` → `.jpg`）
- 幂等：同一张图永远落在同一路径，重复导入不产生新文件
- `image_path` 的 UNIQUE 约束首次变得有实际意义
- 原始文件名与来源路径继续记录在 `original_path`（CLI 导入时为源文件绝对路径）
- 由于采用全库唯一去重，同一 `content_hash` 只属于一个 `model_number`，故该路径全局唯一
- 文件名只取哈希前 16 个十六进制字符（64 bit）。数据库中存的是完整 64 字符哈希且带 UNIQUE 约束，文件名截断仅影响可读性，不承担唯一性保证

---

## 5. 检索路径设计

### 5.1 SQL

```sql
SET LOCAL hnsw.ef_search = :ef;

WITH candidates AS (
    SELECT model_number, image_path, original_path, oss_path,
           vector <=> :query_vector AS distance
    FROM product_images
    ORDER BY vector <=> :query_vector
    LIMIT :fetch_n
), best AS (
    SELECT DISTINCT ON (model_number)
           model_number, image_path, original_path, oss_path, distance
    FROM candidates
    ORDER BY model_number, distance
)
SELECT * FROM best ORDER BY distance LIMIT :top_k;
```

### 5.2 参数

- `fetch_n = clamp(top_k * OVERSAMPLE, top_k, 500)`，`OVERSAMPLE` 默认 5，可由环境变量 `SEARCH_OVERSAMPLE` 覆盖
- `ef = max(fetch_n, 40)`

`OVERSAMPLE = 5` 的依据：过采样系数应大致等于单个产品的平均图片数。当前业务下每个型号通常 3-6 张图，取 5 可使 top_k 个不同产品在绝大多数情况下能被填满。

### 5.3 关键点

- **`SET LOCAL`** 而非 `SET`：Gunicorn + SQLAlchemy 连接池会复用连接，`SET` 会污染后续所有查询。`SET LOCAL` 仅在当前事务内生效。
- **移除 `JOIN Product`**：FK 约束 + `ON DELETE CASCADE` 已保证不存在孤儿图片行，该 join 无实际过滤作用，只增加查询计划选错的风险。
- **相似度夹紧**：`similarity = min(1.0, max(0.0, 1.0 - distance))`。补上界的依据是实测向量 L2 范数为 1.000282 而非精确 1.0，同图余弦相似度会达到 1.00056。
- **删除应用层去重**：`blueprints/products_v2.py` 中的 `dedupe_results_by_model_number` 及其调用一并移除。

### 5.4 已知局限（不视为缺陷）

DISTINCT ON 作用于被截断的候选集（`fetch_n` 条）。若某产品的最佳图片排名在 `fetch_n` 之外，该产品不会出现在结果中。这是近似最近邻检索的固有性质，`fetch_n` / `OVERSAMPLE` 即为该权衡的调节旋钮。文档中如实说明，不宣称精确。

---

## 6. 批量导入 CLI 设计

### 6.1 命令行接口

```bash
python -m scripts.ingest_images --root data/摄像师拍摄素材 \
    [--dry-run] [--rebuild-index] [--batch-size 20] [--limit N]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--root` | `app.config['DATASET_ROOT']` | 素材根目录，其下一级子目录名即 `model_number` |
| `--dry-run` | 关闭 | 只扫描、算哈希、查重并输出报告，不调用 API、不写库、不落盘 |
| `--rebuild-index` | 关闭 | 导入前 DROP HNSW 索引，导入后重建 |
| `--batch-size` | 20 | 每次 DashScope 请求的图片数。传入值会被 clamp 到 `[1, 20]`，20 为实测硬限制 |
| `--limit` | 无 | 只处理前 N 张，用于调试 |

现有 `backend/scripts/ingest_dataset.py` 删除（已废弃且引用不存在的 `VectorProductIndex`）。

### 6.2 执行流程

1. 扫描 `--root` 的**一级子目录**，每个子目录名即一个 `model_number`；在每个型号目录内部**递归**收集图片文件（因此 `CS-001/细节图/a.jpg` 也归属 `CS-001`）。允许的扩展名：`.jpg .jpeg .png .gif .webp`（大小写不敏感）。`--root` 下直接存放的散图不属于任何型号，计入孤儿清单
2. **一次**查询取出所有已有 `model_number`。目录名不在其中的归入孤儿清单，末尾统一报告
3. 对全部待处理图片计算 SHA-256（纯本地，零 API 成本）
4. **一次**查询取出所有已有 `content_hash` 及其对应的 `image_path`，载入内存 dict
5. 过滤：已入库的哈希跳过并记录「与哪张图重复」；同一次运行内重复出现的哈希也跳过（批内去重）
6. 按 `--batch-size` 分批调用 `EmbeddingClient.embed_images`
7. 每批一个数据库事务：落盘 + 写入 `product_images`
8. 输出汇总报告

### 6.3 报告格式

```
扫描: 1,240 张 / 120 个型号目录

  ✓ 入库          1,060 张
  ⊘ 重复跳过        180 张（节省 ¥0.036）
      CS-012/3.jpg  与 CS-001/1.jpg 内容相同
      ...（前 20 条，其余写入 ingest_report.log）
  ✗ 孤儿目录          5 个（无对应产品，已跳过）
      CS-007, CS-08, HL-2, XX-99, TMP
  ✗ 失败              0 张

耗时 94s / 53 个批次 / API 费用约 ¥0.21
```

### 6.4 幂等性

重跑同一目录时，所有哈希均命中数据库，全部跳过，**零 API 调用**。断点续传因此天然成立——中断后重跑即可，无需记录 checkpoint。

### 6.5 批失败降级

一批 20 张中只要有一张损坏图片，整个请求会返回 400，导致 20 张全部失败。处理方式：捕获批级异常后，将该批拆成单张逐个重试，只有真正有问题的图片被记为失败。

### 6.6 `--rebuild-index`

```sql
-- 导入前
DROP INDEX IF EXISTS idx_product_images_vector_hnsw;

-- 导入后
SET maintenance_work_mem = '2GB';
SET max_parallel_maintenance_workers = 7;
CREATE INDEX idx_product_images_vector_hnsw
    ON product_images USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
RESET maintenance_work_mem;
RESET max_parallel_maintenance_workers;
ANALYZE product_images;
```

对应 pgvector 官方的「先加载数据后创建索引」建议。首次全量导入时使用；日常增量导入不使用。

### 6.7 性能预期

按实测 89 ms/张：1,000 张约 90 秒、约 ¥0.2。默认串行执行批次，不引入并发（批量本身已带来 5.5× 提升，KISS 优先）。

---

## 7. 写入路径修复

`ImageIngestService.ingest_one()` 由 `create_product` 与 `update_product` 共同复用，顺带修复以下缺陷：

1. **`update_product` 不再吞异常** —— 向量生成失败时回滚事务、清理已落盘文件、返回 503 `EMBEDDING_SERVICE_ERROR`，与 `create_product` 行为一致
2. **删除时清理磁盘文件** —— `delete_product`、`delete_product_image`、`batch_delete_products` 均先查出 `original_path` 集合，删除数据库行提交成功后再删文件
3. **重复图片返回明确信息** —— 上传已存在的图片时返回 `{"skipped": true, "duplicate_of": "/uploads/product_images/CS-001/a1b2c3d4e5f6a7b8.jpg"}`，而非静默丢弃
4. **限流判断改为 `status_code == 429`** —— 替代字符串匹配 `"rate limit exceeded"`；将 `status_code` 分支与 `except` 分支中重复的两套重试逻辑合并为一处
5. **CSV 导入改为批量提交** —— 一次查出全部已有 `model_number` 做存在性判断，分批 commit（每 200 行），替代当前的逐行 commit + 逐行 `query.get`

---

## 8. 错误处理

保留现有异常类型与 HTTP 映射：

| 异常 | HTTP | error_code |
|---|---|---|
| `EmbeddingServiceError` | 503 | `EMBEDDING_SERVICE_ERROR` |
| `VectorSearchError` | 500 | `VECTOR_SEARCH_ERROR` |
| `ValueError`（top_k 校验） | 400 | `INVALID_TOP_K` |

不新增「重复图片」异常类型——去重是正常业务流程，通过返回值 `skipped` 字段表达，而非抛异常。

重试策略统一在 `EmbeddingClient` 内部：`status_code == 429` 时指数退避（初始 5 s，每次 ×2，最多 3 次），其他错误立即抛出 `EmbeddingServiceError`。

---

## 9. 测试策略

现有 5 个 SQLite 测试（`test/test_products_v2_search_behaviors.py`）保留不动，作为回归基线。

**新增 `backend/test/integration/`（连接 5433 上的 Docker PostgreSQL）**

向量 SQL 至今零测试覆盖——现有测试通过 `FakeSearchService` 把整条检索路径 mock 掉，且 SQLite 只是把 `VECTOR(1024)` 当作未知类型名接受（SQLite 动态类型），真正执行 `cosine_distance` 会失败。这是当前最大的测试窟窿。

| 文件 | 覆盖内容 |
|---|---|
| `integration/test_vector_search.py` | DISTINCT ON 折叠正确性（一个产品 5 张图时 `top_k=3` 必须返回 3 个**不同产品**）；`ef_search` 确实生效；similarity 夹在 [0, 1] 内 |
| `integration/test_dedup.py` | 同哈希跳过；批内去重；跨型号重复触发报告；UNIQUE 约束生效 |
| `test_embedding_batch.py` | 分批切分逻辑；批级失败降级为单张重试（mock DashScope，不产生真实调用） |
| `test_ingest_cli.py` | 孤儿目录识别与报告；`--dry-run` 不写库不调 API；重跑幂等（第二次零 API 调用） |

集成测试使用独立数据库 `image_search_test`，在 fixture 中建表/清表，避免污染开发数据。

---

## 10. 明确不做（YAGNI）

以下项目本次不实现，需要时再单独提案：

- pHash 近似去重（改尺寸/水印/重压缩的同图）
- `halfvec` 半精度与二值量化索引
- `hnsw.iterative_scan`（等真正引入分类过滤后再评估）
- CLI 导入并发（当前串行已足够快）
- 前端批量上传 UI 与 SSE 进度
- 文搜图端点（实测文本向量同为 1024 维、同一向量空间，随时可加）

---

## 11. 交付清单

| 类别 | 内容 |
|---|---|
| 新增 | `backend/services/{__init__,embedding,vector_search,ingest}.py` |
| 新增 | `backend/scripts/ingest_images.py` |
| 新增 | `backend/test/integration/{test_vector_search,test_dedup}.py`、`backend/test/{test_embedding_batch,test_ingest_cli}.py` |
| 修改 | `backend/product_search.py`（缩为兼容层） |
| 修改 | `backend/models/product.py`（`content_hash` 列） |
| 修改 | `postgres/init/01_init.sql`、`backend/init_db.py`（schema 同步） |
| 修改 | `backend/blueprints/products_v2.py`（检索改造 + 4 处 bug 修复 + CSV 批量提交） |
| 删除 | `backend/scripts/ingest_dataset.py` |
| 文档 | 更新 `CLAUDE.md` 的 Schema、CLI、测试章节 |
