# Issue #16 Batch Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让图库管理员把最多 100 张活跃未归款图片资产原子移入回收站，并立即从所有发现型入口隐藏，同时保留版本、审计和恢复所需数据。

**Architecture:** 新增一个拥有完整事务的深模块，先稳定排序锁定全部目标，再全批校验并以一条条件更新完成归档；状态更新、批次活动和逐资产活动同事务提交。HTTP 与 React 只做适配和用户流程编排，所有 active 可见性约束保留在服务端查询中。

**Tech Stack:** Python 3.13、Flask、Flask-SQLAlchemy、PostgreSQL 16、pgvector、pytest、React 18、TypeScript、Ant Design 5、Vitest、React Testing Library。

## Global Constraints

- 基线是 `088bb9f` 加 Issue #15 的 21 个 tracked 与 12 个 untracked delta；不得遗漏或覆盖。
- 只归档 `active + model_number IS NULL`；单批 1–100；重复、缺失、已归款或非法状态使资产全批不变。
- `archived + model_number IS NULL` 是幂等 no-op；不得再次递增 version 或改变 archived_at。
- 成功归档必须 `version + 1`，并与 batch + item 活动记录同事务提交。
- 不删除资产、向量、原图、预览或任何 OSS 对象；不调用 OSS/Kodo/DashScope。
- 所有普通/向量搜索、默认列表和归款候选必须服务端显式过滤 active；archived 私有预览仍允许已知 ID 访问。
- 不连接或写入任何 PostgreSQL，不运行 integration 测试，不执行迁移、部署、云操作或删除。
- 需要 PostgreSQL 的测试只编写并标记为未执行。
- 不 commit、push、修改 GitHub Issue/PR 或合并。
- 用户明确跳过正式 code review；完成前仍执行新鲜定向测试、build、diff 和安全核对。

---

### Task 1: 批量归档深模块

**Files:**
- Create: `backend/services/asset_activity.py`
- Create: `backend/services/asset_archive.py`
- Create: `backend/test/test_asset_archive_unit.py`
- Modify: `backend/services/asset_display_name.py`

**Interfaces:**
- Consumes: Issue #15 的 `AssetActivityRecord`、`ImageAsset.version`、`status` 与 `archived_at`。
- Produces: `archive_unassigned_image_assets(session, asset_ids, *, request_id: str) -> ArchiveBatchResult`。

- [ ] **Step 1: 写第一个失败测试，固定成功与幂等行为**

在 `test_asset_archive_unit.py` 建立 fake session，并写测试：

```python
def test_archives_active_unassigned_and_keeps_archived_retry_unchanged():
    active = _asset(version=3)
    archived = _asset(status='archived', version=7, archived_at=FIXED_ARCHIVE_TIME)
    session = FakeSession(
        locked=[active, archived],
        updated=[_updated(active, version=4)],
    )

    result = archive_unassigned_image_assets(
        session,
        [str(active.id), str(archived.id)],
        request_id='issue-16-request',
    )

    assert result.status == 'succeeded'
    assert result.archived_count == 1
    assert result.already_archived_count == 1
    assert [item.status for item in result.items] == [
        'archived', 'already_archived'
    ]
    assert result.items[0].version == 4
    assert result.items[1].version == 7
    assert archived.archived_at == FIXED_ARCHIVE_TIME
    assert session.commits == 1
    assert session.rollbacks == 0
    assert len(session.added) == 3
```

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
cd backend
env -u DATABASE_URL -u DB_HOST -u DB_PORT -u DB_NAME -u DB_USER -u DB_PASSWORD \
  -u DASHSCOPE_API_KEY -u OSS_ACCESS_KEY_ID -u OSS_ACCESS_KEY_SECRET \
  PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest test/test_asset_archive_unit.py -v
```

Expected: collection FAIL，明确缺少 `services.asset_archive`。

- [ ] **Step 3: 实现最小公开模型与成功路径**

`asset_activity.py` 提供安全摘要：

```python
from collections.abc import Mapping


def activity_state(asset) -> dict:
    def value(name):
        if isinstance(asset, Mapping):
            return asset.get(name)
        return getattr(asset, name)

    return {
        'model_number': value('model_number'),
        'display_name': value('display_name'),
        'version': value('version'),
        'status': value('status'),
    }
```

`asset_archive.py` 定义 `ArchiveItemResult`、`ArchiveBatchResult`、`ArchiveRequestValidationError` 与 `archive_unassigned_image_assets`；实现：

```python
locked = session.execute(
    select(ImageAsset)
    .where(ImageAsset.id.in_(unique_ids))
    .order_by(ImageAsset.id)
    .with_for_update()
).scalars().all()

statement = (
    update(ImageAsset)
    .where(
        ImageAsset.id.in_(eligible_ids),
        ImageAsset.status == 'active',
        ImageAsset.model_number.is_(None),
    )
    .values(
        status='archived',
        archived_at=func.now(),
        updated_at=func.now(),
        version=ImageAsset.version + 1,
    )
    .returning(
        ImageAsset.id,
        ImageAsset.model_number,
        ImageAsset.display_name,
        ImageAsset.version,
        ImageAsset.status,
    )
    .execution_options(synchronize_session=False)
)
```

为成功结果创建一个 `asset.archive.batch` 和每个目标一个 `asset.archive` 记录，再只调用一次 `session.commit()`。把 `asset_display_name.py` 的私有 `_activity_state` 改为导入共享 `activity_state`，保持 Issue #15 记录形状不变。

- [ ] **Step 4: 运行单测试确认 GREEN**

Run Task 1 Step 2 命令。Expected: 当前测试 PASS。

- [ ] **Step 5: 逐个增加冲突与失败测试，并完成每轮 RED→GREEN**

依次加入并单独运行；每个测试的完整输入与断言如下：

| Test | 固定输入 | 必须断言 |
|---|---|---|
| `test_rejects_duplicate_uuid_spellings_and_records_unique_item_result` | 同一 UUID 的标准字符串和大写字符串 | `status == 'rejected'`、零 update、一次 commit、一个 batch 与一个唯一 target 记录、错误码为重复冲突 |
| `test_missing_asset_rejects_the_whole_batch_without_update` | 一个存在的 active 未归款资产和一个随机缺失 UUID | 两项均未 update，存在项结果为 `unchanged`，缺失项为 `rejected/IMAGE_ASSET_NOT_FOUND` |
| `test_assigned_active_asset_rejects_the_whole_batch_without_update` | 一个 active 未归款与一个 active 已归款 | 两项版本与状态不变，已归款项为 `IMAGE_ASSET_ALREADY_ASSIGNED` |
| `test_unknown_or_archived_assigned_state_rejects_the_whole_batch` | 一个 `status='processing'` 与一个 `status='archived', model_number='CS-001'` | 两者均为拒绝原因，零 update，整批只有审计写入 |
| `test_rejected_batch_records_each_valid_target_reason` | eligible、missing、assigned 三个 ID | 一个 batch 加三个 item 记录，共用同一 batch_id，记录只含稳定错误分类 |
| `test_rejects_empty_oversized_non_string_and_invalid_uuid_payloads` | `[]`、101 个 UUID、包含整数、包含非法 UUID | 每个输入抛 `ArchiveRequestValidationError`，零 execute/add/commit，且错误消息不回显原始值 |
| `test_update_count_mismatch_rolls_back_without_commit` | 两个 eligible，但 UPDATE RETURNING 只返回一个 | 抛运行时异常、`commits == 0`、`rollbacks == 1`、无已提交活动记录 |
| `test_activity_failure_rolls_back_the_archive_update` | eligible 目标，fake `add_all` 抛错 | 原异常传播、`commits == 0`、`rollbacks == 1` |
| `test_activity_states_never_contain_vector_or_object_storage_fields` | 资产带 `vector/oss_path/preview_oss_path` 哨兵值 | 遍历 batch/item 的 before/after JSON，均不含这些键或哨兵值 |

每轮只实现使当前测试通过的分支。业务冲突返回 `ArchiveBatchResult(status='rejected')` 并提交拒绝审计；结构错误抛出 `ArchiveRequestValidationError` 且不保存原始输入；意外异常统一 rollback 后重抛。

- [ ] **Step 6: 运行后端纯单元套件**

Run:

```bash
cd backend
env -u DATABASE_URL -u DB_HOST -u DB_PORT -u DB_NAME -u DB_USER -u DB_PASSWORD \
  -u DASHSCOPE_API_KEY -u OSS_ACCESS_KEY_ID -u OSS_ACCESS_KEY_SECRET \
  PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest \
    test/test_asset_archive_unit.py \
    test/test_asset_display_name_unit.py -v
```

Expected: 全 PASS、0 skip、无数据库或云连接。

---

### Task 2: Flask 接口与服务端 active 合同

**Files:**
- Modify: `backend/blueprints/image_assets.py`
- Create: `backend/test/test_issue_16_static_contract.py`

**Interfaces:**
- Consumes: `archive_unassigned_image_assets`。
- Produces: `POST /api/image-assets/archive` 与服务端可见性静态回归合同。

- [ ] **Step 1: 写失败的路由/可见性静态合同**

```python
def test_archive_route_delegates_to_the_transaction_module():
    source = _read(BACKEND_DIR / 'blueprints' / 'image_assets.py')
    assert "@image_assets_bp.post('/archive')" in source
    assert 'archive_unassigned_image_assets(' in source
    assert 'IMAGE_ASSET_ARCHIVE_CONFLICT' in source
    assert 'IMAGE_ASSET_ARCHIVE_FAILED' in source


def test_every_discovery_query_keeps_an_explicit_active_filter():
    management = _read(BACKEND_DIR / 'blueprints' / 'image_assets.py')
    vector = _read(BACKEND_DIR / 'services' / 'vector_search.py')
    products = _read(BACKEND_DIR / 'blueprints' / 'products_v2.py')
    assert "ImageAsset.query.filter(ImageAsset.status == 'active')" in management
    assert "WHERE status = 'active'" in vector
    assert "ImageAsset.status == 'active'" in products
    assert "if any(asset.status != 'active'" in management
    assert '.order_by(ImageAsset.id).with_for_update()' in management


def test_archive_module_has_no_delete_storage_or_embedding_path():
    source = _read(BACKEND_DIR / 'services' / 'asset_archive.py')
    assert 'session.delete' not in source
    assert 'OssObjectStorage' not in source
    assert 'EmbeddingClient' not in source
    assert '.oss_path =' not in source
    assert '.preview_oss_path =' not in source
    assert '.vector =' not in source
```

- [ ] **Step 2: 运行静态合同确认 RED**

Run:

```bash
cd backend
PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest test/test_issue_16_static_contract.py -v
```

Expected: 路由合同 FAIL，active 与无删除合同保持可诊断。

- [ ] **Step 3: 实现最小 HTTP 适配并统一批量归款锁顺序**

```python
@image_assets_bp.post('/archive')
def archive_image_assets():
    payload = request.get_json(silent=True)
    request_id = (request.headers.get('X-Request-ID') or str(uuid.uuid4()))[:64]
    try:
        result = archive_unassigned_image_assets(
            db.session,
            payload.get('asset_ids') if isinstance(payload, dict) else None,
            request_id=request_id,
        )
    except ArchiveRequestValidationError as exc:
        db.session.rollback()
        return jsonify({'error': str(exc), 'error_code': exc.error_code}), 400
    except Exception as exc:
        db.session.rollback()
        logger.error(
            'image_asset.archive.failed request_id=%s error_type=%s',
            request_id,
            type(exc).__name__,
        )
        return jsonify({
            'error': '图片移入回收站失败，请稍后重试',
            'error_code': 'IMAGE_ASSET_ARCHIVE_FAILED',
        }), 500

    payload = result.to_dict()
    return jsonify(payload), 200 if result.status == 'succeeded' else 409
```

同时把既有归款读取改为：

```python
assets = (
    ImageAsset.query
    .filter(ImageAsset.id.in_(asset_ids))
    .order_by(ImageAsset.id)
    .with_for_update()
    .all()
)
```

这只统一多资产命令的锁顺序，不改变归款状态或返回语义。

- [ ] **Step 4: 运行静态合同与 Issue #15 合同**

Run:

```bash
cd backend
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  test/test_issue_16_static_contract.py \
  test/test_issue_15_static_contract.py -v
```

Expected: 全 PASS。

---

### Task 3: 前端 transport 合同

**Files:**
- Modify: `frontend/src/types/product.ts`
- Modify: `frontend/src/services/productApi.ts`
- Modify: `frontend/src/services/productApi.test.ts`

**Interfaces:**
- Consumes: `POST /api/image-assets/archive`。
- Produces: `archiveImageAssets(assetIds: string[]): Promise<ImageAssetArchiveResponse>`。

- [ ] **Step 1: 写失败的 fake-fetch 测试**

```typescript
it('posts selected ids to the atomic archive endpoint', async () => {
  const response = {
    batch_id: 'batch-1', status: 'succeeded' as const,
    archived_count: 2, already_archived_count: 0, items: [],
  };
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true, json: async () => response,
  });
  vi.stubGlobal('fetch', fetchMock);

  await expect(archiveImageAssets(['asset-1', 'asset-2']))
    .resolves.toEqual(response);
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining('/api/image-assets/archive'),
    expect.objectContaining({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ asset_ids: ['asset-1', 'asset-2'] }),
    })
  );
});
```

再写一条非 2xx 测试，断言后端 `error` 原样抛出。

- [ ] **Step 2: 运行单文件确认 RED**

Run:

```bash
cd frontend
npm test -- --run src/services/productApi.test.ts
```

Expected: FAIL，缺少 export。

- [ ] **Step 3: 增加精确类型和最小 adapter**

```typescript
export interface ImageAssetArchiveItemResult {
  asset_id: string;
  status: 'archived' | 'already_archived' | 'unchanged' | 'rejected';
  version: number | null;
  error_code?: string;
  error?: string;
}

export interface ImageAssetArchiveResponse {
  batch_id: string;
  status: 'succeeded' | 'rejected';
  archived_count: number;
  already_archived_count: number;
  items: ImageAssetArchiveItemResult[];
  error_code?: string;
  error?: string;
}
```

```typescript
export const archiveImageAssets = async (
  assetIds: string[]
): Promise<ImageAssetArchiveResponse> => {
  const response = await fetch(`${API_BASE_URL}/api/image-assets/archive`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ asset_ids: assetIds }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || '图片移入回收站失败');
  }
  return payload;
};
```

- [ ] **Step 4: 重跑 adapter 测试确认 GREEN**

Run Task 3 Step 2 命令。Expected: 全 PASS。

---

### Task 4: 产品管理顶层归档流程

**Files:**
- Modify: `frontend/src/components/UnassignedAssetGrid.tsx`
- Modify: `frontend/src/components/UnassignedAssetGrid.test.tsx`
- Modify: `frontend/src/components/ProductUpload.tsx`
- Modify: `frontend/src/components/ProductUpload.test.tsx`

**Interfaces:**
- Consumes: `archiveImageAssets`。
- Produces: 选择后出现的批量动作、明确确认、成功刷新与失败保留。

- [ ] **Step 1: 写顶层失败测试**

在 service mock 中增加 `archiveImageAssets: vi.fn()`，然后写：

```typescript
it('archives selected assets only after an explicit searchable-impact confirmation', async () => {
  vi.mocked(api.archiveImageAssets).mockResolvedValue({
    batch_id: 'batch-1', status: 'succeeded', archived_count: 1,
    already_archived_count: 0, items: [],
  });
  render(<ProductUpload />);

  expect(screen.queryByRole('button', { name: '移入回收站' }))
    .not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole('checkbox'));
  fireEvent.click(screen.getByRole('button', { name: '移入回收站' }));

  expect(screen.getByText(/普通搜索/)).toBeInTheDocument();
  expect(screen.getByText(/向量搜索/)).toBeInTheDocument();
  expect(screen.getByText(/可从回收站恢复/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '确认移入回收站' }));

  await waitFor(() => expect(api.archiveImageAssets)
    .toHaveBeenCalledWith(['asset-1']));
  await waitFor(() => expect(api.getImageAssets).toHaveBeenCalledTimes(2));
  expect(screen.queryByRole('button', { name: '移入回收站' }))
    .not.toBeInTheDocument();
});
```

再逐个加入：取消不调用 API；失败显示后端原因、保留选择且不刷新；只有未归款 checkbox 可选。

- [ ] **Step 2: 运行两个组件文件确认 RED**

Run:

```bash
cd frontend
npm test -- --run \
  src/components/UnassignedAssetGrid.test.tsx \
  src/components/ProductUpload.test.tsx
```

Expected: 新测试 FAIL，既有 Issue #15 测试仍提供回归信号。

- [ ] **Step 3: 实现受控确认流程**

`UnassignedAssetGrid` 新增 `onArchive`，并只在有选择时渲染整个 `.asset-batch-actions`。保留已归款 checkbox 禁用。

`ProductUpload` 新增：

```typescript
const [archiveModalOpen, setArchiveModalOpen] = useState(false);
const [archiving, setArchiving] = useState(false);

const handleArchiveAssets = async () => {
  if (selectedAssetIds.length === 0) return;
  setArchiving(true);
  try {
    const result = await archiveImageAssets(selectedAssetIds);
    message.success(
      `已处理 ${result.archived_count + result.already_archived_count} 张图片`
    );
    setArchiveModalOpen(false);
    setSelectedAssetIds([]);
    await fetchAssets(assetPage, assetSearch, assetAssignment);
  } catch (error) {
    message.error(
      error instanceof Error ? error.message : '图片移入回收站失败'
    );
  } finally {
    setArchiving(false);
  }
};
```

确认框固定显示：

```tsx
<Modal
  title={`确认将选中的 ${selectedAssetIds.length} 张图片移入回收站？`}
  open={archiveModalOpen}
  okText="确认移入回收站"
  cancelText="取消"
  okButtonProps={{ danger: true }}
  confirmLoading={archiving}
  onOk={handleArchiveAssets}
  onCancel={() => setArchiveModalOpen(false)}
>
  <p>
    移入后，这些图片将立即从普通搜索和向量搜索中隐藏，
    但不会删除原图、预览或向量，仍可从回收站恢复。
  </p>
</Modal>
```

- [ ] **Step 4: 重跑组件测试确认 GREEN**

Run Task 4 Step 2 命令。Expected: 全 PASS。

---

### Task 5: 编写真实 PostgreSQL 场景但不执行

**Files:**
- Create: `backend/test/integration/test_issue_16_batch_archive.py`

**Interfaces:**
- Consumes: Flask 外部接口、真实独立 PostgreSQL、`VectorSearchService.search_by_vector`。
- Produces: 后续经用户授权可执行的事务与 pgvector 验收。

- [ ] **Step 1: 标记模块需要 PostgreSQL 授权**

```python
"""Issue #16 PostgreSQL scenarios; not executed without explicit approval."""

import pytest

pytestmark = pytest.mark.postgresql
```

- [ ] **Step 2: 编写业务场景**

完整覆盖以下可执行场景；测试帮助函数复用现有 integration fixture 的 `_asset` 形状，所有断言均从 HTTP 或持久行观察：

| Test | 操作 | 持久/公开断言 |
|---|---|---|
| `test_batch_archive_updates_status_time_version_and_audit_atomically` | POST 两个 active 未归款 ID | 200；两行 archived_at 非空且相同事务时间、version 从 1 到 2；向量和 OSS 键逐值不变；恰好 1 batch + 2 item 记录 |
| `test_assigned_or_missing_target_keeps_every_asset_unchanged` | POST eligible、assigned 与 missing ID | 409；所有存在行 status/version/archived_at 不变；逐项原因齐全；只有拒绝审计新增 |
| `test_duplicate_target_keeps_every_asset_unchanged_and_returns_reasons` | 同一 UUID 以两种字符串形式提交 | 409；资产不变；响应与审计标明重复冲突且每个唯一目标只记录一次 |
| `test_retry_is_idempotent_without_second_version_or_time_change` | 同一单项请求连续成功提交两次 | 第二次 `archived_count=0/already_archived_count=1`；version 与 archived_at 等于第一次；第二次 item activity 结果为 noop |
| `test_archived_assets_leave_text_default_assignment_and_vector_results` | 归档后调用 `GET /api/image-assets` 的空搜索、显示名称搜索、来源路径搜索，并以固定 1024 维向量调用 `search_by_vector` | 四个发现结果均不含该 asset_id；不调用 embedding |
| `test_activity_insert_failure_rolls_back_every_asset_update` | monkeypatch session flush/commit 使活动 INSERT 失败 | HTTP 500；刷新后全部资产仍 active、version=1、archived_at 为空 |
| `test_archived_asset_preview_remains_private_and_available` | 注入 fake signing storage 后归档，再 GET preview | 302 到 fake 私有签名地址；资产仍 archived；没有 OSS 写/delete 调用 |

每个场景通过 Flask 响应、公开搜索结果和持久行断言；向量场景直接调用 `search_by_vector([0.1] * 1024)`，不生成查询 embedding、不调用云服务。活动记录断言 batch + 每项目标，并确认 JSONB 不含向量、对象键、凭证或签名 URL。

- [ ] **Step 3: 只做静态人工检查，不 collect、不执行**

确认文件位于 `test/integration`、带 `pytest.mark.postgresql`，并在最终报告列为“编写但未运行”。

---

### Task 6: 新鲜安全验收与风险核对

**Files:**
- Modify only if verification exposes Issue #16 defects.

**Interfaces:**
- Produces: 可复制的新鲜测试、build、diff 和未验证项证据。

- [ ] **Step 1: 运行后端纯单元/静态范围**

```bash
cd backend
env -u DATABASE_URL -u DB_HOST -u DB_PORT -u DB_NAME -u DB_USER -u DB_PASSWORD \
  -u DASHSCOPE_API_KEY -u OSS_ACCESS_KEY_ID -u OSS_ACCESS_KEY_SECRET \
  -u OSS_ENDPOINT -u OSS_BUCKET_NAME -u QINIU_ACCESS_KEY \
  -u QINIU_SECRET_KEY -u QINIU_BUCKET_NAME -u QINIU_REGION \
  PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest \
    test/test_asset_archive_unit.py \
    test/test_issue_16_static_contract.py \
    test/test_asset_display_name_unit.py \
    test/test_issue_15_static_contract.py -v
```

Expected: 全 PASS、0 skip，不触发 integration、数据库或云适配器。

- [ ] **Step 2: 运行前端 fake-API/组件范围与 build**

```bash
cd frontend
npm test -- --run \
  src/services/productApi.test.ts \
  src/components/AssetDisplayNameEditor.test.tsx \
  src/components/UnassignedAssetGrid.test.tsx \
  src/components/ProductUpload.test.tsx \
  src/components/ProductSearch.test.tsx
npm run build
```

Expected: 全 PASS，`tsc && vite build` exit 0。

- [ ] **Step 3: 执行静态安全核对**

```bash
git diff --check
rg -n "delete\(|session\.delete|DROP|OssObjectStorage|EmbeddingClient" \
  backend/services/asset_archive.py backend/blueprints/image_assets.py
rg -n "status = 'active'|ImageAsset\.status == 'active'" \
  backend/services/vector_search.py \
  backend/blueprints/image_assets.py \
  backend/blueprints/products_v2.py
git status --short --untracked-files=all
```

Expected: 无 diff 格式错误；归档模块无删除/OSS/embedding；所有发现型查询仍有 active；无 commit。

- [ ] **Step 4: 对照规格自审并汇报**

逐项核对 100 上限、全有或全无、幂等、version、活动记录、active 过滤、无物理删除与前端确认。明确列出：真实 PostgreSQL 事务/锁/pgvector/回滚测试未运行；Issue #15 migration 未执行；既有产品单图归档和同源重传再激活旁路仍由后续 Ticket 处理。
