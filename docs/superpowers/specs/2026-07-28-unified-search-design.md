# 统一搜索设计文档：关键词搜索 + 自然语言智能搜索

- 日期：2026-07-28
- 状态：设计已确认，待实施
- 范围：在现有"以图搜款"基础上，新增关键词搜索与自然语言语义搜索，前端统一为一个搜索入口、三种模式。

---

## 1. 背景与调研结论

### 1.1 现状

- Embedding 模型：DashScope `tongyi-embedding-vision-plus-2026-03-06`（多模态，图文同空间），1024 维，通过 dashscope SDK 的 `MultiModalEmbedding.call()` 调用。定义在 `backend/product_search.py`（`EMBEDDING_MODEL` / `EMBEDDING_DIMENSION`）。
- 向量存储：PostgreSQL 16 + pgvector，`product_images.vector vector(1024)`，已建 HNSW 索引（`vector_cosine_ops`, m=16, ef_construction=64），见 `postgres/init/01_init.sql`。
- 现有搜索：仅 `POST /api/products/search`（以图搜图），逻辑在 `product_search.py` 的 `ImageSearchService.search_similar_images()`：提取图向量 → cosine_distance 检索 → 按 model_number 去重 → similarity = 1 - distance。
- 列表接口 `GET /api/products?search=xxx` 目前仅对 `model_number`、`category` 做 LIKE 匹配（`backend/blueprints/products_v2.py`）。
- 前端：`ProductSearch.tsx` 为真实以图搜款组件；`productApi.ts` 提供 `searchProductsByImage()` 与 `getProducts()`。无文本搜索 UI。

### 1.2 关键可行性结论

- 同一 DashScope 模型支持 `{'text': '...'}` 输入，文本向量与图片向量同空间——**文本向量可直接检索现有图片向量库，无需换模型、无需重新入库、无需数据库迁移**。
- 商品表已有丰富文本字段（`model_number`、`category`、`spec_cn`、`spec_en`、`product_size`、`package_size`）可用于关键词搜索。
- 数据规模为几千～几万条：ILIKE 模糊匹配性能可接受，暂不建全文索引（预留优化点）。

### 1.3 已确认的产品决策

| 决策点 | 结论 |
|---|---|
| 前端形态 | 统一搜索入口 + 模式切换（以图搜款 / 关键词搜索 / 智能搜索），用户手动切换模式 |
| 语义检索路线 | 实时生成查询文本向量，直接检索现有图片向量（不预计算商品文本向量） |
| 关键词匹配范围 | 全部文本字段 |
| 数据规模 | 几千～几万，ILIKE 够用，预留索引优化注释 |

---

## 2. 后端设计

### 2.1 关键词搜索（扩展现有接口）

**接口**：`GET /api/products`（扩展现有 `search` 参数行为，路由与响应结构不变）

规则：

1. `search` 参数按空白字符拆分为多个关键词。
2. 多个关键词之间为 AND 关系；单个关键词命中以下任一字段即算命中（OR）：
   - `model_number`、`category`、`spec_cn`、`spec_en`、`product_size`、`package_size`
3. 匹配使用 `ILIKE '%kw%'`（不区分大小写）。NULL 字段安全跳过。
4. 分页、排序、`category` 过滤等现有行为完全保留；响应 JSON 结构不变。
5. 在查询代码处保留注释：数据量超过 ~10 万行时，改用 `pg_trgm` GIN 索引优化。

**示例**：`GET /api/products?search=相机肩带 纯棉&page=1&per_page=20`
→ 返回同时命中"相机肩带"和"纯棉"（各自命中任一字段）的商品分页列表。

### 2.2 自然语言语义搜索（新增接口）

**新增方法（`backend/product_search.py`）**：

- `extract_text_feature(self, text: str, request_id=None) -> np.ndarray`
  - 调用 `dashscope.MultiModalEmbedding.call(model=EMBEDDING_MODEL, input=[{'text': text}], dimension=EMBEDDING_DIMENSION)`
  - 复用现有的 API key 配置、错误处理与日志模式（与 `extract_feature` 对称）。
- `search_by_text(self, query_text: str, top_k: int = 10, min_similarity: float = 0.0, request_id=None) -> list`
  - 文本向量 → `ProductImage.vector.cosine_distance()` 检索 → join `Product` → 按 `model_number` 去重（保留距离最近一条）→ `similarity = max(0, 1 - distance)` → 过滤 `similarity >= min_similarity`。
  - 结果条目结构与 `search_similar_images()` 完全一致。

**新增路由（`backend/blueprints/products_v2.py`）**：`POST /api/products/search/text`

请求（JSON）：

```json
{
  "query": "适合户外的宽版纯棉相机背带",
  "top_k": 10,
  "min_similarity": 0.2
}
```

- `query`：必填，非空字符串（trim 后判空），最大长度 500 字符。
- `top_k`：可选，默认 10，上限 50。
- `min_similarity`：可选，默认 0.2（宽松阈值，可调）。

响应（200，与以图搜款响应结构一致）：

```json
{
  "results": [
    {
      "model_number": "CS-001",
      "category": "相机肩带",
      "similarity": 0.83,
      "matched_image": "/uploads/product_images/....png",
      "...": "其余 Product 序列化字段与以图搜款一致"
    }
  ]
}
```

> 注：若现有以图搜款接口返回的是数组而非 `{results: [...]}` 包装，则文本搜索接口与其保持完全一致的顶层结构，以现有实现为准——两个接口响应结构必须相同，前端结果组件复用。

错误处理：

| 场景 | 状态码 | 响应 |
|---|---|---|
| `query` 缺失/空白 | 400 | `{"error": "query 不能为空"}` |
| DashScope 调用失败/超时 | 502 | `{"error": "文本向量服务调用失败，请稍后重试"}`（后台记录详细日志） |
| 其他异常 | 500 | 与现有接口错误格式一致 |

### 2.3 数据库变更

**无**。不新增表、不新增字段、不新增索引。仅在关键词查询代码中留注释说明未来 pg_trgm 优化路径。

---

## 3. 前端设计

### 3.1 组件改造（`frontend/src/components/ProductSearch.tsx`）

- 顶部新增模式切换（Segmented / Radio.Group 风格，与现有 UI 库一致）：
  - **以图搜款**：现有上传图片 + 搜索流程，保持不动。
  - **关键词搜索**：文本输入框，占位提示如"输入货号、类目或参数关键词，空格分隔多个词"；回车或按钮触发；调用 `getProducts({ search, page, per_page })`；结果以商品列表/卡片展示（无相似度），支持分页。
  - **智能搜索**：同一文本输入框，占位提示如"用一句话描述你想找的商品，如：适合户外的宽版纯棉相机背带"；调用新的 `searchProductsByText()`；结果卡片复用以图搜款的展示（含相似度徽标、匹配图片）。
- 切换模式时清空上一模式的结果与输入，避免状态串扰。
- 空结果态：给出友好提示（关键词模式提示换关键词；智能模式提示换种描述方式）。

### 3.2 API 服务层（`frontend/src/services/productApi.ts`）

新增：

```typescript
export const searchProductsByText = async (
  query: string,
  topK: number = 10,
  minSimilarity: number = 0.2
): Promise<ProductSearchResult[]> => {
  // POST ${API_BASE_URL}/api/products/search/text
  // body: { query, top_k: topK, min_similarity: minSimilarity }
  // 错误处理与 searchProductsByImage 保持一致
};
```

类型：复用 `ProductSearchResult`（`frontend/src/types/product.ts`），无需新增类型。

---

## 4. 测试策略

### 4.1 后端单元/接口测试（pytest，放在 `backend/test/`）

- `extract_text_feature`：mock dashscope 调用，验证输入构造（`{'text': ...}`）、维度 1024、异常传播。
- 关键词搜索：
  - 单关键词命中不同字段（货号 / 类目 / spec_cn / spec_en / 尺寸）。
  - 多关键词 AND 语义（两个词命中不同字段的商品被返回；只命中一个词的不返回）。
  - 大小写不敏感；NULL 字段不报错；分页正确。
- `POST /api/products/search/text`：
  - 正常查询返回结构与以图搜款一致（mock 向量服务）。
  - 空 query / 缺失 query → 400。
  - `min_similarity` 过滤生效。
  - DashScope 失败 → 502。

### 4.2 端到端验证（浏览器）

- 启动前后端，三种模式各执行一次真实搜索：
  - 以图搜款回归：上传样图，返回结果正常（确认未被本次改动破坏）。
  - 关键词：输入已知货号/类目关键词，结果命中且分页可用。
  - 智能搜索：输入自然语言描述，返回带相似度的结果卡片。
- 检查模式切换时结果清空、空结果提示、错误提示（如后端停掉时的报错文案）。

---

## 5. 验收标准

1. `GET /api/products?search=...` 支持全文本字段、多关键词 AND 匹配，现有调用方行为不回归。
2. `POST /api/products/search/text` 按本文档契约工作，响应结构与以图搜款一致。
3. 前端统一搜索入口三模式可用，以图搜款功能无回归。
4. 后端 pytest 全部通过；浏览器端到端三模式验证通过并留有证据。
5. 无数据库 schema 变更；无临时调试代码残留。

## 6. 明确不做（本期范围外）

- 图文融合搜索（图 + 文一起搜）。
- 商品文本向量预计算、tsvector/pg_trgm 索引落地（仅留注释）。
- 后端自动意图路由（自动判断关键词 vs 语义）。

## 7. 风险与假设

- DashScope 文本输入按官方文档开箱可用；实施第一步须用真实 API 冒烟验证一次 `{'text': ...}` 输入返回 1024 维向量（防止 SDK 版本差异），再继续编码。
- 语义搜索每次查询产生一次 DashScope 调用，成本与延迟（约 100–500ms）在当前使用频率下可接受。
- `min_similarity` 默认 0.2 为经验值，上线后可根据实际效果调整。
