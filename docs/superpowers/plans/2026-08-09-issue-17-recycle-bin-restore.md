# Issue #17 Recycle Bin and Restore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use test-driven development for every production change. This delegated run is executed continuously in the current isolated worktree; the user's explicit constraints override the usual commit and formal code-review steps.

**Goal:** 让图库管理员在独立回收站中只读浏览、双字段搜索和私有预览 archived 图片，并以最多 100 项的全有或全无事务无损恢复。

**Architecture:** 新增 `services.asset_recycle_bin` 深模块，用两个 interface 集中 archived 查询与原子恢复；Flask 蓝图保持 transport adapter，前端新增专用 `ArchivedAssetGrid` 并由 `ProductUpload` 拥有页面状态。恢复复用现有活动记录和 preview seam，不引入对象存储、embedding、迁移或清除依赖。

**Tech Stack:** Python 3 / Flask / SQLAlchemy / PostgreSQL 16 + pgvector（真实场景只编写不执行）；React 18 / TypeScript / Ant Design / Vitest / React Testing Library。

## Global Constraints

- 当前 HEAD 保持 `088bb9f189a93af8573cb53d2bb2e28e54511229`，保留已精确移植的 Issue #15/#16 累计 delta。
- 禁止连接任何 PostgreSQL、OSS、Kodo 或 DashScope；不得加载这些系统的凭证。
- 只执行纯单元/mock/静态合同、前端 fake-API/组件测试、TypeScript build 与 diff 检查。
- 真实 PostgreSQL 事务、锁、回滚、排序与 pgvector 场景只编写，并在最终报告明确为未执行。
- 不迁移、不部署、不删除、不 commit、不 push、不修改 GitHub Issue/PR。
- 恢复最多 100 项且全有或全无；actual restore 设置 `status='active'`、`archived_at=NULL`、`updated_at=NOW()`、`version=version+1`。
- actual restore 的 `model_number` 必须保持 NULL；ID、display name、source identity、vector、OSS binding 和 `created_at` 不变。
- active 目标是幂等 no-op；即使已在恢复后归款，也不得因请求重试撤销归款。
- 不实现自动过期、永久清除、重新上传、预览重建或 embedding。
- 正式 `code-review` 按用户要求跳过；完成声明前仍必须运行新鲜验证。

---

### Task 1: 锁定后端 RED 合同

**Files:**
- Create: `backend/test/test_asset_recycle_bin_unit.py`
- Create: `backend/test/test_issue_17_static_contract.py`
- Create: `backend/test/integration/test_issue_17_recycle_bin.py`

**Interfaces:**
- Consumes: Issue #15 `management_asset_dict`、`activity_state`；Issue #16 的 fake-session 事务测试形状。
- Produces: `list_archived_image_assets(session, *, page, per_page, search)` 与
  `restore_image_assets(session, asset_ids, *, request_id)` 的可执行行为合同。

- [ ] **Step 1: 写列表单元合同**

覆盖未筛选 `archived_total`、筛选后 `total`、安全 DTO 中 `archived_at`、查询不 commit，以及编译后的 SQL 含 `status='archived'`、双字段 `ILIKE`、`archived_at DESC NULLS LAST` 和 `id DESC`。

```python
page = list_archived_image_assets(
    session, page=2, per_page=24, search='A%_\\'
)
assert page.total == 1
assert page.archived_total == 7
assert page.assets[0]['archived_at'] == '2026-08-09T12:00:00'
assert session.commits == 0
```

- [ ] **Step 2: 写恢复状态矩阵合同**

至少包含以下精确测试名，每项各自建立独立 fake session 和断言：

- `test_restores_archived_and_keeps_active_retry_idempotent`
- `test_active_assigned_retry_is_noop_and_preserves_assignment`
- `test_missing_duplicate_invalid_or_archived_assigned_rejects_all`
- `test_rejects_empty_oversized_non_string_and_invalid_uuid_payloads`
- `test_update_count_mismatch_rolls_back_without_activity_commit`
- `test_activity_or_commit_failure_rolls_back_every_restore`
- `test_restore_activity_never_contains_vector_or_object_fields`

断言成功时一条 batch + 每目标一条 item 活动记录，事件为 `asset.restore.batch` / `asset.restore`；active 项为 `noop`。

- [ ] **Step 3: 写静态安全合同**

断言独立 `/archived` 与 `/restore` 路由、active 发现过滤仍存在、恢复模块不包含 `session.delete`、`OssObjectStorage`、`EmbeddingClient`、过期或 purge 入口，并只出现允许的生命周期写字段。

- [ ] **Step 4: 写真实 PostgreSQL 场景但不执行**

集成文件标记 `pytest.mark.postgresql`，覆盖：默认排序与双字段搜索、私有预览、成功恢复的身份逐字段不变、mixed active 幂等、冲突整批不变、活动失败回滚，以及恢复后普通搜索、向量搜索和待归款列表重新可见。

- [ ] **Step 5: 运行 RED**

Run:

```bash
cd backend
python -m pytest test/test_asset_recycle_bin_unit.py test/test_issue_17_static_contract.py -q
```

Expected: FAIL/ERROR，原因是 `services.asset_recycle_bin` 与两个新路由尚不存在；不得运行 `test/integration/test_issue_17_recycle_bin.py`。

### Task 2: 实现后端深模块与 transport adapter

**Files:**
- Create: `backend/services/asset_recycle_bin.py`
- Modify: `backend/blueprints/image_assets.py`
- Modify: `backend/services/asset_display_name.py`

**Interfaces:**
- Consumes: Task 1 的公开测试 seam；`AssetActivityRecord`、`activity_state`、`management_asset_dict`。
- Produces: `GET /api/image-assets/archived`、`POST /api/image-assets/restore`。

- [ ] **Step 1: 实现归档页结果与查询**

结果类型必须是冻结 dataclass `ArchivedAssetPage`，字段按顺序固定为
`assets: list[dict]`、`total: int`、`archived_total: int`、`page: int`、
`per_page: int`；查询入口签名固定为
`list_archived_image_assets(session, *, page: int, per_page: int, search: str) -> ArchivedAssetPage`。

使用两个 count 语义（无 search 时复用未筛选 count），分页采用 `OFFSET/LIMIT`；查询异常只向 route 传播，不提交事务。

- [ ] **Step 2: 实现恢复结果、校验与事务**

```python
@dataclass(frozen=True)
class RestoreItemResult:
    asset_id: str
    status: str
    version: int | None = None
    error_code: str | None = None
    error: str | None = None

@dataclass(frozen=True)
class RestoreBatchResult:
    batch_id: str
    status: str
    restored_count: int
    already_active_count: int
    items: list[RestoreItemResult]
```

按设计状态矩阵完成稳定锁、先判定后更新、更新数核对、活动记录与单次 commit/rollback。非法 shape 抛出 `RestoreRequestValidationError`，不包含原始无效 ID。

- [ ] **Step 3: 扩展安全管理表示**

`management_asset_dict` 增加：

```python
'archived_at': archived_at.isoformat() if archived_at else None,
```

不得增加 OSS key、向量或签名 URL。

- [ ] **Step 4: 实现 Flask adapter**

`/archived` 只校验分页并调用列表 interface；`/restore` 的 HTTP 映射固定为 200/400/409/500 与设计中的稳定错误码。异常日志只记录 request ID 与异常类型。

- [ ] **Step 5: 运行 GREEN 与父回归**

Run:

```bash
cd backend
python -m pytest \
  test/test_asset_display_name_unit.py \
  test/test_issue_15_static_contract.py \
  test/test_asset_archive_unit.py \
  test/test_issue_16_static_contract.py \
  test/test_asset_recycle_bin_unit.py \
  test/test_issue_17_static_contract.py -q
```

Expected: 全部通过；integration 文件仍不执行。

### Task 3: 锁定前端 transport 与回收站组件 RED

**Files:**
- Modify: `frontend/src/services/productApi.test.ts`
- Create: `frontend/src/components/ArchivedAssetGrid.test.tsx`
- Modify: `frontend/src/components/ProductUpload.test.tsx`

**Interfaces:**
- Consumes: 后端 `/archived`、`/restore` wire contract。
- Produces: `getArchivedImageAssets`、`restoreImageAssets` 与顶层用户流程合同。

- [ ] **Step 1: 写 transport 失败测试**

```ts
await getArchivedImageAssets({ page: 2, perPage: 24, search: '中文 空格' });
expect(fetch).toHaveBeenCalledWith(
  expect.stringContaining('/api/image-assets/archived?page=2&per_page=24'),
  { method: 'GET' }
);

await restoreImageAssets(['asset-1', 'asset-2']);
expect(fetch).toHaveBeenCalledWith(
  expect.stringContaining('/api/image-assets/restore'),
  expect.objectContaining({ body: JSON.stringify({ asset_ids: ['asset-1', 'asset-2'] }) })
);
```

错误响应必须把逐项中文 `error` 汇总为用户可理解信息。

- [ ] **Step 2: 写叶子组件失败测试**

覆盖私有 preview URL、只读显示名、双字段搜索、分页、多选恢复、assigned archived 禁选，以及界面不存在改名/永久清除入口。

- [ ] **Step 3: 写 ProductUpload 顶层失败测试**

覆盖：首次加载回收站数量；切换独立标签；搜索；批量恢复成功后刷新 active + archived 并同步数量；失败后选择保留；归档成功刷新 archived 数量。

- [ ] **Step 4: 运行 RED**

Run:

```bash
cd frontend
npm test -- --run \
  src/services/productApi.test.ts \
  src/components/ArchivedAssetGrid.test.tsx \
  src/components/ProductUpload.test.tsx
```

Expected: FAIL，原因是新类型、transport、组件与顶层状态尚不存在。

### Task 4: 实现前端回收站流程

**Files:**
- Modify: `frontend/src/types/product.ts`
- Modify: `frontend/src/services/productApi.ts`
- Create: `frontend/src/components/ArchivedAssetGrid.tsx`
- Modify: `frontend/src/components/ProductUpload.tsx`
- Modify if needed: `frontend/src/index.css`

**Interfaces:**
- Consumes: Task 3 测试；Task 2 后端 wire contract。
- Produces: 独立回收站标签、数量、搜索、预览和批量恢复用户流程。

- [ ] **Step 1: 增加稳定 TypeScript 表示**

新增 `ArchivedImageAssetListResponse`、`ImageAssetRestoreItemResult`、`ImageAssetRestoreResponse`，并为管理资产增加 `archived_at: string | null`。

- [ ] **Step 2: 实现 fetch adapter**

`getArchivedImageAssets` 编码 page/per_page/search；`restoreImageAssets` POST JSON。非 2xx 时优先组合 `items[].error`，否则使用顶层 `error`。

- [ ] **Step 3: 实现只读 ArchivedAssetGrid**

复用现有 CSS 与 `getImageUrl`；不渲染 `AssetDisplayNameEditor`。选择工具条只提供恢复，assigned archived 卡片禁选并标注。

- [ ] **Step 4: 在 ProductUpload 集成独立状态**

新增 archived assets、filtered total、global count、page、search、loading、error、selection、restoring 状态。首次并行加载 active 和 archived；归档或恢复成功刷新两侧。恢复失败不清空选择。

- [ ] **Step 5: 运行 GREEN 与父回归**

Run:

```bash
cd frontend
npm test -- --run \
  src/services/productApi.test.ts \
  src/components/AssetDisplayNameEditor.test.tsx \
  src/components/UnassignedAssetGrid.test.tsx \
  src/components/ArchivedAssetGrid.test.tsx \
  src/components/ProductUpload.test.tsx \
  src/components/ProductSearch.test.tsx
```

Expected: 六个文件全部通过。

### Task 5: 文档与最终允许范围验证

**Files:**
- Modify: `AGENTS.md`
- Verify: all Issue #15/#16/#17 changed files

**Interfaces:**
- Consumes: 完成的后端/前端合同。
- Produces: 可交接的架构入口、测试原始证据与未验证项清单。

- [ ] **Step 1: 更新最近作用域架构事实**

只记录 `asset_recycle_bin`、独立 archived 路由、恢复不变量和前端回收站入口；不复制实现过程，不改主工作树的 CONTEXT/ADR。

- [ ] **Step 2: 运行新鲜后端定向套件**

重复 Task 2 的六文件命令并保存准确通过数、耗时和 warning。不得运行 integration。

- [ ] **Step 3: 运行新鲜前端定向套件与 build**

重复 Task 4 的六文件命令，然后：

```bash
cd frontend
npm run build
```

- [ ] **Step 4: 运行 diff 与安全边界检查**

```bash
git diff --check
git status --short --untracked-files=all
rg -n "session.delete|OssObjectStorage|EmbeddingClient|permanent.?purge|自动过期" \
  backend/services/asset_recycle_bin.py \
  frontend/src/components/ArchivedAssetGrid.tsx
```

Expected: `git diff --check` 退出 0；安全扫描只命中说明性否定文本或零命中，不存在删除、云写、重传或 embedding 路径。

- [ ] **Step 5: 核对冲突与父 delta**

以实现前保存的 40 文件父 manifest 为锚点，确认父 worktree 和主工作树未变化；当前 worktree 的额外 delta 全部可归因于 Issue #17。不得还原用户或父任务改动。

- [ ] **Step 6: 最终报告**

逐项列出：父 delta 核验、Issue #17 变更文件、新鲜测试原始结果、真实 PostgreSQL/pgvector/事务未执行、云/迁移/部署/删除未执行、正式 code review 已按要求跳过，以及冲突状态。
