# Unassigned Image Assets Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有产品管理页中默认展示可搜索、可分页的待归款图片，并允许把选中图片事务化关联到已有真实产品型号。

**Architecture:** `image_assets` 蓝图增加安全的管理列表 DTO 和批量关联命令；列表绝不复用包含 OSS 内部字段的模型 `to_dict()`。前端把待归款卡片网格拆成独立组件，由 `ProductUpload` 负责产品/图片两个视图的编排、计数刷新和关联弹窗。现有私有预览 302、产品 CRUD、CSV 导入和 Issue #12 的旧路径收缩保持不变。

**Tech Stack:** Python 3.9+、Flask、Flask-SQLAlchemy、PostgreSQL 16/pgvector、pytest、React 18、TypeScript、Ant Design 5、Tailwind CSS、Vitest、Testing Library、Vite。

## Global Constraints

- 不从目录、文件名或图片内容推断型号、分类或商品资料。
- 不自动创建临时型号或待完善产品。
- 不实现解除关联、跨型号改绑、归档、永久删除或 OSS 清理。
- 图片管理响应不得暴露 OSS Object Key、Bucket、内容哈希或签名 URL。
- 正式预览只使用 `/api/image-assets/<asset_id>/preview` 的短时签名 302。
- 不恢复或依赖 `product_images`、本地 `uploads` 或公开图片 URL；实现须适配 Issue #12 收缩后的模型与测试夹具。
- 后端行为严格执行 red → green；前端先建立 Vitest 测试 seam，再为每个组件行为执行 red → green。

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `backend/blueprints/image_assets.py` | 安全分页列表和批量关联已有型号；保留现有私有预览。 |
| `backend/test/integration/test_image_asset_management.py` | 在真实 PostgreSQL 中锁定列表、隐私字段、搜索、分页和事务关联行为。 |
| `frontend/src/types/product.ts` | 图片资产管理 DTO、分页响应和关联响应类型。 |
| `frontend/src/services/productApi.ts` | `getImageAssets()` 与 `assignImageAssets()` HTTP 边界。 |
| `frontend/src/services/productApi.test.ts` | 验证查询参数、请求体和错误响应。 |
| `frontend/src/components/UnassignedAssetGrid.tsx` | 待归款图片搜索、卡片、选择、分页、空/错状态。 |
| `frontend/src/components/UnassignedAssetGrid.test.tsx` | 组件展示、搜索、选择与无产品禁用行为。 |
| `frontend/src/components/ProductUpload.tsx` | 双视图编排、真实计数、加载数据和关联弹窗。 |
| `frontend/src/components/ProductUpload.test.tsx` | 默认待归款视图及成功关联后的刷新行为。 |
| `frontend/src/test/setup.ts` | jsdom 浏览器 API 垫片和 jest-dom 断言。 |
| `frontend/vite.config.ts` | Vitest jsdom 与 setup 文件配置。 |
| `frontend/package.json`、`frontend/package-lock.json` | 固定测试依赖与 `test` 脚本。 |

---

### Task 1: Add the safe image-asset management list

**Files:**

- Create: `backend/test/integration/test_image_asset_management.py`
- Modify: `backend/blueprints/image_assets.py`

**Interfaces:**

- Produces: `GET /api/image-assets?assignment=unassigned&page=1&per_page=24&search=<path>`.
- Produces: `{assets, total, page, per_page}` and only the safe fields specified below.

- [ ] **Step 1: Write the fixture helper and failing list tests**

```python
# backend/test/integration/test_image_asset_management.py
import uuid

from models import ImageAsset, Product, db


def _product(model_number='CS-001'):
    return Product(
        model_number=model_number,
        photographer_file='摄影师文件',
        alibaba_product_url='https://example.com/product',
        category='挂绳',
    )


def _asset(path, *, model_number=None, status='active'):
    digest = uuid.uuid5(uuid.NAMESPACE_URL, path).hex.ljust(64, '0')
    return ImageAsset(
        model_number=model_number,
        source_provider='qiniu-kodo',
        source_bucket='xiangxipackage',
        source_relative_path=path,
        source_revision=1,
        oss_path=f'image-search/xiangxipackage/{path}',
        preview_oss_path=(
            f'image-search/previews/preview-v1/{digest[:2]}/{digest}.jpg'
        ),
        content_hash=digest,
        source_size=4096,
        source_mime_type='image/png',
        source_width=1200,
        source_height=800,
        vector=[0.1] * 1024,
        embedding_model='tongyi-embedding-vision-plus-2026-03-06',
        embedding_dimension=1024,
        normalization_version='preview-v1',
        status=status,
    )


def test_lists_only_active_unassigned_assets_with_safe_fields(app):
    db.session.add(_product())
    db.session.add_all([
        _asset('中文 目录/待归款.png'),
        _asset('已有型号/图片.png', model_number='CS-001'),
        _asset('归档/图片.png', status='archived'),
    ])
    db.session.commit()

    response = app.test_client().get('/api/image-assets')

    assert response.status_code == 200
    body = response.get_json()
    assert body['total'] == 1
    assert body['page'] == 1
    assert body['per_page'] == 24
    assert [item['source_relative_path'] for item in body['assets']] == [
        '中文 目录/待归款.png'
    ]
    item = body['assets'][0]
    assert set(item) == {
        'asset_id', 'model_number', 'source_relative_path', 'preview_url',
        'source_size', 'source_mime_type', 'source_width', 'source_height',
        'created_at',
    }
    assert item['preview_url'] == (
        f"/api/image-assets/{item['asset_id']}/preview"
    )
    private_fields = {
        'oss_path', 'preview_oss_path', 'source_bucket', 'content_hash'
    }
    assert not private_fields & set(item)


def test_filters_assignment_search_and_paginates(app):
    db.session.add(_product())
    db.session.add_all([
        _asset('中文 空格/第一页.png'),
        _asset('中文 空格/第二页.png'),
        _asset('其他/不匹配.png'),
        _asset('已归款/匹配.png', model_number='CS-001'),
    ])
    db.session.commit()

    client = app.test_client()
    unassigned = client.get(
        '/api/image-assets?assignment=unassigned&search=中文 空格&page=1&per_page=1'
    ).get_json()
    assigned = client.get(
        '/api/image-assets?assignment=assigned&search=匹配'
    ).get_json()
    all_assets = client.get('/api/image-assets?assignment=all').get_json()

    assert unassigned['total'] == 2
    assert len(unassigned['assets']) == 1
    assert assigned['total'] == 1
    assert assigned['assets'][0]['model_number'] == 'CS-001'
    assert all_assets['total'] == 4


def test_rejects_invalid_management_list_parameters(app):
    client = app.test_client()
    assert client.get('/api/image-assets?assignment=unknown').status_code == 400
    assert client.get('/api/image-assets?page=0').status_code == 400
    assert client.get('/api/image-assets?per_page=101').status_code == 400
```

- [ ] **Step 2: Run tests and verify RED**

```bash
cd backend
set -a && source .env && set +a
python -m pytest test/integration/test_image_asset_management.py -v
```

Expected: the tests fail because `GET /api/image-assets` is not implemented.

- [ ] **Step 3: Implement the minimal safe list**

Add `request` to the Flask imports and add before the preview route:

```python
MANAGEMENT_ASSIGNMENTS = frozenset({'unassigned', 'assigned', 'all'})


def _management_asset_dict(asset):
    return {
        'asset_id': str(asset.id),
        'model_number': asset.model_number,
        'source_relative_path': asset.source_relative_path,
        'preview_url': f'/api/image-assets/{asset.id}/preview',
        'source_size': asset.source_size,
        'source_mime_type': asset.source_mime_type,
        'source_width': asset.source_width,
        'source_height': asset.source_height,
        'created_at': asset.created_at.isoformat() if asset.created_at else None,
    }


def _request_integer(name, default, minimum, maximum):
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if minimum <= value <= maximum else None


@image_assets_bp.get('')
def list_image_assets():
    assignment = request.args.get('assignment', 'unassigned')
    page = _request_integer('page', 1, 1, 1_000_000)
    per_page = _request_integer('per_page', 24, 1, 100)
    if assignment not in MANAGEMENT_ASSIGNMENTS or page is None or per_page is None:
        return jsonify({
            'error': '图片资产列表参数无效',
            'error_code': 'INVALID_IMAGE_ASSET_LIST_PARAMS',
        }), 400

    query = ImageAsset.query.filter(ImageAsset.status == 'active')
    if assignment == 'unassigned':
        query = query.filter(ImageAsset.model_number.is_(None))
    elif assignment == 'assigned':
        query = query.filter(ImageAsset.model_number.isnot(None))
    search = (request.args.get('search') or '').strip()
    if search:
        query = query.filter(ImageAsset.source_relative_path.ilike(f'%{search}%'))

    pagination = query.order_by(
        ImageAsset.created_at.desc(), ImageAsset.id.desc()
    ).paginate(page=page, per_page=per_page, error_out=False)
    return jsonify({
        'assets': [_management_asset_dict(asset) for asset in pagination.items],
        'total': pagination.total,
        'page': page,
        'per_page': per_page,
    })
```

- [ ] **Step 4: Run the list tests and verify GREEN**

Run the Step 2 command. Expected: 3 passed.

- [ ] **Step 5: Commit the list slice**

```bash
git add backend/blueprints/image_assets.py backend/test/integration/test_image_asset_management.py
git commit -m "feat(assets): list unassigned images safely"
```

---

### Task 2: Add transactional assignment to an existing product

**Files:**

- Modify: `backend/test/integration/test_image_asset_management.py`
- Modify: `backend/blueprints/image_assets.py`

**Interfaces:**

- Produces: `POST /api/image-assets/assign` with `{asset_ids: string[], model_number: string}`.
- Success: `{model_number, assigned_count, reused_count}`.
- Conflict is all-or-nothing and never reassigns an asset from another model.

- [ ] **Step 1: Add failing assignment tests**

```python
def test_assigns_multiple_unassigned_assets_in_one_transaction(app):
    product = _product()
    first = _asset('待归款/一.png')
    second = _asset('待归款/二.png')
    db.session.add_all([product, first, second])
    db.session.commit()
    response = app.test_client().post('/api/image-assets/assign', json={
        'asset_ids': [str(first.id), str(second.id)],
        'model_number': product.model_number,
    })
    assert response.status_code == 200
    assert response.get_json() == {
        'model_number': 'CS-001', 'assigned_count': 2, 'reused_count': 0,
    }
    db.session.expire_all()
    assert db.session.get(ImageAsset, first.id).model_number == 'CS-001'
    assert db.session.get(ImageAsset, second.id).model_number == 'CS-001'


def test_assignment_is_idempotent_for_the_same_model(app):
    product = _product()
    asset = _asset('已归款/图片.png', model_number='CS-001')
    db.session.add_all([product, asset])
    db.session.commit()
    response = app.test_client().post('/api/image-assets/assign', json={
        'asset_ids': [str(asset.id)], 'model_number': 'CS-001',
    })
    assert response.status_code == 200
    assert response.get_json()['assigned_count'] == 0
    assert response.get_json()['reused_count'] == 1


def test_assignment_conflict_rolls_back_the_whole_batch(app):
    first_product = _product('CS-001')
    second_product = _product('CS-002')
    free_asset = _asset('待归款/保持未归款.png')
    conflict = _asset('已归款/冲突.png', model_number='CS-002')
    db.session.add_all([first_product, second_product, free_asset, conflict])
    db.session.commit()
    response = app.test_client().post('/api/image-assets/assign', json={
        'asset_ids': [str(free_asset.id), str(conflict.id)],
        'model_number': 'CS-001',
    })
    assert response.status_code == 409
    assert response.get_json()['error_code'] == 'IMAGE_ASSET_ASSIGNMENT_CONFLICT'
    db.session.expire_all()
    assert db.session.get(ImageAsset, free_asset.id).model_number is None
    assert db.session.get(ImageAsset, conflict.id).model_number == 'CS-002'


def test_assignment_rejects_missing_product_asset_and_archived_asset(app):
    product = _product()
    archived = _asset('归档/图片.png', status='archived')
    db.session.add_all([product, archived])
    db.session.commit()
    client = app.test_client()
    missing_product = client.post('/api/image-assets/assign', json={
        'asset_ids': [str(archived.id)], 'model_number': 'NOT-FOUND',
    })
    missing_asset = client.post('/api/image-assets/assign', json={
        'asset_ids': [str(uuid.uuid4())], 'model_number': 'CS-001',
    })
    archived_asset = client.post('/api/image-assets/assign', json={
        'asset_ids': [str(archived.id)], 'model_number': 'CS-001',
    })
    assert missing_product.status_code == 404
    assert missing_product.get_json()['error_code'] == 'PRODUCT_NOT_FOUND'
    assert missing_asset.status_code == 404
    assert missing_asset.get_json()['error_code'] == 'IMAGE_ASSET_NOT_FOUND'
    assert archived_asset.status_code == 409
    assert archived_asset.get_json()['error_code'] == 'IMAGE_ASSET_NOT_ACTIVE'


def test_assignment_rejects_invalid_or_duplicate_asset_ids(app):
    client = app.test_client()
    asset_id = str(uuid.uuid4())
    assert client.post('/api/image-assets/assign', json={}).status_code == 400
    assert client.post('/api/image-assets/assign', json={
        'asset_ids': [asset_id, asset_id], 'model_number': 'CS-001',
    }).status_code == 400
    assert client.post('/api/image-assets/assign', json={
        'asset_ids': ['not-a-uuid'], 'model_number': 'CS-001',
    }).status_code == 400
```

- [ ] **Step 2: Run assignment tests and verify RED**

```bash
cd backend
set -a && source .env && set +a
python -m pytest test/integration/test_image_asset_management.py -k assignment -v
```

Expected: tests fail because the assignment route is missing.

- [ ] **Step 3: Implement validation and the transactional route**

Add `uuid`, `request`, and `Product` imports, then add:

```python
MAX_ASSIGNMENT_BATCH = 100


def _assignment_error(message, error_code, status):
    db.session.rollback()
    return jsonify({'error': message, 'error_code': error_code}), status


@image_assets_bp.post('/assign')
def assign_image_assets():
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get('asset_ids')
    model_number = payload.get('model_number')
    if (
        not isinstance(raw_ids, list)
        or not 1 <= len(raw_ids) <= MAX_ASSIGNMENT_BATCH
        or not isinstance(model_number, str)
        or not model_number.strip()
        or any(not isinstance(value, str) for value in raw_ids)
        or len(set(raw_ids)) != len(raw_ids)
    ):
        return _assignment_error(
            '关联参数无效', 'INVALID_IMAGE_ASSET_ASSIGNMENT', 400
        )
    try:
        asset_ids = [uuid.UUID(value) for value in raw_ids]
    except (TypeError, ValueError, AttributeError):
        return _assignment_error(
            '图片资产 ID 无效', 'INVALID_IMAGE_ASSET_ASSIGNMENT', 400
        )

    model_number = model_number.strip()
    if db.session.get(Product, model_number) is None:
        return _assignment_error(
            '目标型号不存在，请刷新产品列表', 'PRODUCT_NOT_FOUND', 404
        )
    assets = ImageAsset.query.filter(
        ImageAsset.id.in_(asset_ids)
    ).with_for_update().all()
    if len(assets) != len(asset_ids):
        return _assignment_error(
            '图片资产不存在', 'IMAGE_ASSET_NOT_FOUND', 404
        )
    if any(asset.status != 'active' for asset in assets):
        return _assignment_error(
            '归档图片不能关联型号', 'IMAGE_ASSET_NOT_ACTIVE', 409
        )
    if any(asset.model_number not in (None, model_number) for asset in assets):
        return _assignment_error(
            '图片已关联其他型号，未修改本批数据',
            'IMAGE_ASSET_ASSIGNMENT_CONFLICT', 409,
        )

    assigned_count = 0
    reused_count = 0
    for asset in assets:
        if asset.model_number == model_number:
            reused_count += 1
        else:
            asset.model_number = model_number
            assigned_count += 1
    db.session.commit()
    return jsonify({
        'model_number': model_number,
        'assigned_count': assigned_count,
        'reused_count': reused_count,
    })
```

- [ ] **Step 4: Run all management tests and verify GREEN**

Run Task 1 Step 2. Expected: all management tests pass.

- [ ] **Step 5: Commit the assignment slice**

```bash
git add backend/blueprints/image_assets.py backend/test/integration/test_image_asset_management.py
git commit -m "feat(assets): assign images to existing models"
```

---

### Task 3: Establish frontend tests and add typed API calls

**Files:**

- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `frontend/vite.config.ts`
- Create: `frontend/src/test/setup.ts`
- Modify: `frontend/src/types/product.ts`
- Modify: `frontend/src/services/productApi.ts`
- Create: `frontend/src/services/productApi.test.ts`

**Interfaces:**

- Produces: `getImageAssets(params): Promise<ImageAssetListResponse>`.
- Produces: `assignImageAssets(assetIds, modelNumber): Promise<ImageAssetAssignmentResponse>`.

- [ ] **Step 1: Install and configure the test seam**

```bash
cd frontend
npm install --save-dev vitest jsdom @testing-library/react @testing-library/jest-dom
```

Add `"test": "vitest"` to `package.json`. Replace `vite.config.ts` with:

```typescript
/// <reference types="vitest/config" />

import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  optimizeDeps: {
    exclude: ['lucide-react'],
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    open: false,
    cors: true,
  },
  preview: {
    host: '0.0.0.0',
    port: 4173,
    strictPort: true,
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: true,
    css: true,
  },
});
```

Create the setup file:

```typescript
// frontend/src/test/setup.ts
import '@testing-library/jest-dom/vitest';

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }),
});

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver = (
  ResizeObserverStub as unknown as typeof ResizeObserver
);
```

- [ ] **Step 2: Write failing API-service tests**

```typescript
// frontend/src/services/productApi.test.ts
import { afterEach, describe, expect, it, vi } from 'vitest';
import { assignImageAssets, getImageAssets } from './productApi';

afterEach(() => vi.unstubAllGlobals());

describe('image asset management API', () => {
  it('requests one filtered unassigned page', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ assets: [], total: 0, page: 2, per_page: 24 }),
    });
    vi.stubGlobal('fetch', fetchMock);
    await getImageAssets({ page: 2, perPage: 24, search: '中文 空格' });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining(
        '/api/image-assets?assignment=unassigned&page=2&per_page=24&search='
      ),
      { method: 'GET' }
    );
  });

  it('posts selected ids and the real model number', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        model_number: 'CS-001', assigned_count: 2, reused_count: 0,
      }),
    });
    vi.stubGlobal('fetch', fetchMock);
    await assignImageAssets(['asset-1', 'asset-2'], 'CS-001');
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/image-assets/assign'),
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          asset_ids: ['asset-1', 'asset-2'], model_number: 'CS-001',
        }),
      })
    );
  });

  it('surfaces the backend assignment error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      json: async () => ({ error: '目标型号不存在，请刷新产品列表' }),
    }));
    await expect(assignImageAssets(['asset-1'], 'MISSING')).rejects.toThrow(
      '目标型号不存在，请刷新产品列表'
    );
  });
});
```

- [ ] **Step 3: Run API tests and verify RED**

```bash
npm test -- --run src/services/productApi.test.ts
```

Expected: collection/transform fails because the two exports do not exist.

- [ ] **Step 4: Add exact frontend DTOs and services**

Add to `types/product.ts`:

```typescript
export interface ImageAssetManagementItem {
  asset_id: string;
  model_number: string | null;
  source_relative_path: string;
  preview_url: string;
  source_size: number;
  source_mime_type: string;
  source_width: number;
  source_height: number;
  created_at: string | null;
}

export interface ImageAssetListResponse {
  assets: ImageAssetManagementItem[];
  total: number;
  page: number;
  per_page: number;
}

export interface ImageAssetAssignmentResponse {
  model_number: string;
  assigned_count: number;
  reused_count: number;
}
```

Import these types in `productApi.ts` and add:

```typescript
export const getImageAssets = async (params: {
  page: number;
  perPage: number;
  search?: string;
}): Promise<ImageAssetListResponse> => {
  const query = new URLSearchParams({
    assignment: 'unassigned',
    page: String(params.page),
    per_page: String(params.perPage),
  });
  if (params.search) query.set('search', params.search);
  const response = await fetch(`${API_BASE_URL}/api/image-assets?${query}`, {
    method: 'GET',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({
      error: '获取待归款图片失败',
    }));
    throw new Error(error.error || '获取待归款图片失败');
  }
  return response.json();
};

export const assignImageAssets = async (
  assetIds: string[], modelNumber: string
): Promise<ImageAssetAssignmentResponse> => {
  const response = await fetch(`${API_BASE_URL}/api/image-assets/assign`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ asset_ids: assetIds, model_number: modelNumber }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: '关联型号失败' }));
    throw new Error(error.error || '关联型号失败');
  }
  return response.json();
};
```

- [ ] **Step 5: Run API tests and verify GREEN**

Run Step 3. Expected: 3 passed.

- [ ] **Step 6: Commit typed API slice**

```bash
git add frontend/package.json frontend/package-lock.json frontend/vite.config.ts frontend/src/test/setup.ts frontend/src/types/product.ts frontend/src/services/productApi.ts frontend/src/services/productApi.test.ts
git commit -m "test(frontend): add image management API seam"
```

---

### Task 4: Build the focused unassigned-asset grid

**Files:**

- Create: `frontend/src/components/UnassignedAssetGrid.test.tsx`
- Create: `frontend/src/components/UnassignedAssetGrid.tsx`
- Modify: `frontend/src/index.css` only for focused asset-card classes.

**Interfaces:**

- Consumes: one page of safe management DTOs and callbacks owned by `ProductUpload`.
- Produces: searchable/selectable 24-card page; it performs no HTTP calls itself.

- [ ] **Step 1: Write failing component tests**

```tsx
// frontend/src/components/UnassignedAssetGrid.test.tsx
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { UnassignedAssetGrid } from './UnassignedAssetGrid';

const asset = {
  asset_id: 'asset-1',
  model_number: null,
  source_relative_path: '中文 空格/大图.png',
  preview_url: '/api/image-assets/asset-1/preview',
  source_size: 58_896_865,
  source_mime_type: 'image/png',
  source_width: 6000,
  source_height: 4000,
  created_at: '2026-08-02T11:30:00',
};

const baseProps = {
  assets: [asset], total: 2419, page: 1, pageSize: 24,
  loading: false, error: null, search: '',
  selectedAssetIds: [] as string[], canAssign: false,
  onSearch: vi.fn(), onPageChange: vi.fn(),
  onSelectionChange: vi.fn(), onAssign: vi.fn(), onRetry: vi.fn(),
};

describe('UnassignedAssetGrid', () => {
  it('shows path, dimensions, size and private preview', () => {
    render(<UnassignedAssetGrid {...baseProps} />);
    expect(screen.getByText('中文 空格/大图.png')).toBeInTheDocument();
    expect(screen.getByText('6000 × 4000')).toBeInTheDocument();
    expect(screen.getByText('56.2 MB')).toBeInTheDocument();
    expect(screen.getByRole('img')).toHaveAttribute(
      'src', expect.stringContaining('/api/image-assets/asset-1/preview')
    );
  });

  it('selects a card and disables assignment when no product exists', () => {
    const onSelectionChange = vi.fn();
    render(<UnassignedAssetGrid
      {...baseProps} onSelectionChange={onSelectionChange}
    />);
    fireEvent.click(screen.getByRole('checkbox'));
    expect(onSelectionChange).toHaveBeenCalledWith(['asset-1']);
    expect(screen.getByRole('button', { name: '关联型号' })).toBeDisabled();
  });

  it('submits path search and page changes', () => {
    const onSearch = vi.fn();
    const onPageChange = vi.fn();
    render(<UnassignedAssetGrid
      {...baseProps} onSearch={onSearch} onPageChange={onPageChange}
    />);
    const input = screen.getByPlaceholderText('搜索来源路径');
    fireEvent.change(input, { target: { value: '中文 空格' } });
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' });
    expect(onSearch).toHaveBeenCalledWith('中文 空格');
    fireEvent.click(screen.getByTitle('2'));
    expect(onPageChange).toHaveBeenCalledWith(2);
  });
});
```

- [ ] **Step 2: Run component tests and verify RED**

```bash
cd frontend
npm test -- --run src/components/UnassignedAssetGrid.test.tsx
```

Expected: collection fails because the component is missing.

- [ ] **Step 3: Implement the focused component**

Create this public contract:

```tsx
export interface UnassignedAssetGridProps {
  assets: ImageAssetManagementItem[];
  total: number;
  page: number;
  pageSize: number;
  loading: boolean;
  error: string | null;
  search: string;
  selectedAssetIds: string[];
  canAssign: boolean;
  onSearch: (value: string) => void;
  onPageChange: (page: number) => void;
  onSelectionChange: (assetIds: string[]) => void;
  onAssign: () => void;
  onRetry: () => void;
}
```

Use deterministic helpers:

```tsx
const formatBytes = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
};

const toggleSelection = (
  current: string[], assetId: string, checked: boolean
): string[] => checked
  ? [...current, assetId]
  : current.filter((id) => id !== assetId);
```

The render tree must include:

```tsx
<Input.Search
  placeholder="搜索来源路径"
  value={draftSearch}
  allowClear
  onChange={(event) => setDraftSearch(event.target.value)}
  onSearch={(value) => onSearch(value.trim())}
/>
<Button
  type="primary"
  icon={<LinkOutlined />}
  disabled={selectedAssetIds.length === 0 || !canAssign}
  onClick={onAssign}
>
  关联型号
</Button>
<div className="asset-card-grid">
  {assets.map((asset) => (
    <article key={asset.asset_id} className="asset-card">
      <Checkbox
        checked={selectedAssetIds.includes(asset.asset_id)}
        onChange={(event) => onSelectionChange(toggleSelection(
          selectedAssetIds, asset.asset_id, event.target.checked
        ))}
      />
      <img
        src={getImageUrl(asset.preview_url)}
        alt={asset.source_relative_path}
        loading="lazy"
      />
      <div title={asset.source_relative_path}>{asset.source_relative_path}</div>
      <span>{asset.source_width} × {asset.source_height}</span>
      <span>{formatBytes(asset.source_size)}</span>
    </article>
  ))}
</div>
```

Wrap the grid in Ant Design `Spin`; use `Alert` plus retry for errors, `Empty` for a successful empty page, and `Pagination` with `pageSize={24}`, `showQuickJumper`, and `showTotal={(value) => `共 ${value} 张`}`. Keep styling within the existing teal/amber warm-paper theme.

- [ ] **Step 4: Run component tests and verify GREEN**

Run Step 2. Expected: 3 passed.

- [ ] **Step 5: Commit component slice**

```bash
git add frontend/src/components/UnassignedAssetGrid.tsx frontend/src/components/UnassignedAssetGrid.test.tsx frontend/src/index.css
git commit -m "feat(frontend): add unassigned asset grid"
```

---

### Task 5: Integrate the dual-view product management workbench

**Files:**

- Create: `frontend/src/components/ProductUpload.test.tsx`
- Modify: `frontend/src/components/ProductUpload.tsx`

**Interfaces:**

- `ProductUpload` owns fetching and mutation; `UnassignedAssetGrid` stays presentational.
- Default view key is `assets`; product view key is `products`.

- [ ] **Step 1: Write failing page-level tests**

```tsx
// frontend/src/components/ProductUpload.test.tsx
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ProductUpload } from './ProductUpload';
import * as api from '../services/productApi';

vi.mock('../services/productApi', () => ({
  getProducts: vi.fn(),
  getImageAssets: vi.fn(),
  assignImageAssets: vi.fn(),
  createProduct: vi.fn(),
  updateProduct: vi.fn(),
  deleteProductImage: vi.fn(),
  deleteProduct: vi.fn(),
  batchDeleteProducts: vi.fn(),
  importProductsFromCSV: vi.fn(),
  downloadCSVTemplate: vi.fn(),
  buildVectorIndex: vi.fn(() => () => undefined),
  getImageUrl: (path: string) => path,
}));

const assetResponse = {
  assets: [{
    asset_id: 'asset-1',
    model_number: null,
    source_relative_path: '手机挂绳/A47/修改后/2.png',
    preview_url: '/api/image-assets/asset-1/preview',
    source_size: 58_896_865,
    source_mime_type: 'image/png',
    source_width: 6000,
    source_height: 4000,
    created_at: '2026-08-02T11:30:00',
  }],
  total: 2419,
  page: 1,
  per_page: 24,
};

describe('ProductUpload unified management view', () => {
  beforeEach(() => {
    vi.mocked(api.getProducts).mockResolvedValue({
      products: [], total: 0, page: 0, per_page: 20,
    });
    vi.mocked(api.getImageAssets).mockResolvedValue(assetResponse);
  });

  it('defaults to real unassigned assets when there are no products', async () => {
    render(<ProductUpload />);
    expect(await screen.findByText(
      '手机挂绳/A47/修改后/2.png'
    )).toBeInTheDocument();
    expect(screen.getByText('2,419 张待归款图片')).toBeInTheDocument();
    expect(screen.getByText('0 个产品')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '关联型号' })).toBeDisabled();
    expect(api.getImageAssets).toHaveBeenCalledWith({
      page: 1, perPage: 24, search: '',
    });
  });

  it('refreshes both lists after a successful assignment', async () => {
    vi.mocked(api.getProducts).mockResolvedValue({
      products: [{
        model_number: 'CS-001', photographer_file: 'p',
        alibaba_product_url: 'https://example.com', category: '挂绳',
      }],
      total: 1, page: 0, per_page: 20,
    });
    vi.mocked(api.assignImageAssets).mockResolvedValue({
      model_number: 'CS-001', assigned_count: 1, reused_count: 0,
    });
    render(<ProductUpload />);

    fireEvent.click(await screen.findByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '关联型号' }));
    fireEvent.mouseDown(screen.getByRole('combobox'));
    fireEvent.click(await screen.findByText('CS-001'));
    fireEvent.click(screen.getByRole('button', { name: '确定关联' }));

    await waitFor(() => expect(api.assignImageAssets).toHaveBeenCalledWith(
      ['asset-1'], 'CS-001'
    ));
    await waitFor(() => expect(api.getImageAssets).toHaveBeenCalledTimes(2));
    expect(api.getProducts).toHaveBeenCalledTimes(2);
  });
});
```

- [ ] **Step 2: Run page tests and verify RED**

```bash
cd frontend
npm test -- --run src/components/ProductUpload.test.tsx
```

Expected: first test fails because the current page has no asset view/count; second fails because no assignment flow exists.

- [ ] **Step 3: Add dual-view state and data loading**

Import `Segmented`, `Select`, `UnassignedAssetGrid`, `getImageAssets`, `assignImageAssets`, and `ImageAssetManagementItem`. Add:

```tsx
const ASSET_PAGE_SIZE = 24;
type ManagementView = 'assets' | 'products';

const [activeView, setActiveView] = useState<ManagementView>('assets');
const [assets, setAssets] = useState<ImageAssetManagementItem[]>([]);
const [assetTotal, setAssetTotal] = useState(0);
const [assetPage, setAssetPage] = useState(1);
const [assetSearch, setAssetSearch] = useState('');
const [assetLoading, setAssetLoading] = useState(false);
const [assetError, setAssetError] = useState<string | null>(null);
const [selectedAssetIds, setSelectedAssetIds] = useState<string[]>([]);
const [assignModalOpen, setAssignModalOpen] = useState(false);
const [targetModelNumber, setTargetModelNumber] = useState<string>();
const [assigning, setAssigning] = useState(false);

const fetchAssets = async (page = assetPage, search = assetSearch) => {
  setAssetLoading(true);
  setAssetError(null);
  try {
    const result = await getImageAssets({
      page, perPage: ASSET_PAGE_SIZE, search,
    });
    setAssets(result.assets);
    setAssetTotal(result.total);
  } catch (error) {
    setAssetError(
      error instanceof Error ? error.message : '获取待归款图片失败'
    );
  } finally {
    setAssetLoading(false);
  }
};
```

The initial `useEffect` calls both `fetchProducts()` and `fetchAssets(1, '')`. Search resets page and selection; page changes clear selection. The refresh button calls both loaders.

- [ ] **Step 4: Render counts, view switch and assignment modal**

Replace the single count with:

```tsx
<Tag bordered={false} className="bg-amber-50 text-amber-700 font-medium">
  {assetTotal.toLocaleString('zh-CN')} 张待归款图片
</Tag>
<Tag bordered={false} className="bg-teal-50 text-teal-700 font-medium">
  {products.length.toLocaleString('zh-CN')} 个产品
</Tag>
```

Render the view switch:

```tsx
<Segmented
  value={activeView}
  onChange={(value) => {
    setActiveView(value as ManagementView);
    setSelectedAssetIds([]);
    setSelectedRowKeys([]);
  }}
  options={[
    {
      label: `待归款图片 (${assetTotal.toLocaleString('zh-CN')})`,
      value: 'assets',
    },
    {
      label: `产品资料 (${products.length.toLocaleString('zh-CN')})`,
      value: 'products',
    },
  ]}
/>
```

For `activeView === 'assets'`, render `UnassignedAssetGrid` with all state/callback props. For `products`, render the existing product selection bar, progress block and `Table` unchanged.

Add the transaction trigger:

```tsx
const handleAssignAssets = async () => {
  if (!targetModelNumber || selectedAssetIds.length === 0) return;
  setAssigning(true);
  try {
    const result = await assignImageAssets(
      selectedAssetIds, targetModelNumber
    );
    message.success(
      `已将 ${result.assigned_count + result.reused_count} 张图片关联到 ${targetModelNumber}`
    );
    setAssignModalOpen(false);
    setTargetModelNumber(undefined);
    setSelectedAssetIds([]);
    await Promise.all([
      fetchAssets(assetPage, assetSearch), fetchProducts(),
    ]);
  } catch (error) {
    message.error(error instanceof Error ? error.message : '关联型号失败');
  } finally {
    setAssigning(false);
  }
};
```

Modal:

```tsx
<Modal
  title={`关联 ${selectedAssetIds.length} 张图片`}
  open={assignModalOpen}
  okText="确定关联"
  cancelText="取消"
  confirmLoading={assigning}
  okButtonProps={{ disabled: !targetModelNumber }}
  onOk={handleAssignAssets}
  onCancel={() => {
    setAssignModalOpen(false);
    setTargetModelNumber(undefined);
  }}
>
  <Select
    showSearch
    value={targetModelNumber}
    placeholder="选择真实产品型号"
    optionFilterProp="label"
    options={products.map((product) => ({
      value: product.model_number,
      label: product.model_number,
    }))}
    onChange={setTargetModelNumber}
    className="w-full"
  />
</Modal>
```

- [ ] **Step 5: Run frontend tests and build; verify GREEN**

```bash
npm test -- --run src/services/productApi.test.ts src/components/UnassignedAssetGrid.test.tsx src/components/ProductUpload.test.tsx
npm run build
```

Expected: all tests pass and the production build succeeds.

- [ ] **Step 6: Commit integrated page**

```bash
git add frontend/src/components/ProductUpload.tsx frontend/src/components/ProductUpload.test.tsx
git commit -m "feat(frontend): show unassigned images in product management"
```

---

### Task 6: Full regression and real-browser acceptance

**Files:**

- Modify only when a verified defect is first reproduced by a failing test.

**Interfaces:**

- Verifies the complete HTTP → React → private preview flow against the current 2,419-asset database.

- [ ] **Step 1: Run backend regressions**

```bash
cd backend
set -a && source .env && set +a
python -m pytest test/ -q
```

Expected: all tests pass; only already-known deprecation warnings may remain.

- [ ] **Step 2: Run frontend regressions**

```bash
cd frontend
npm test -- --run
npm run build
```

Expected: all Vitest tests and the production build pass.

- [ ] **Step 3: Rebuild and start the application**

```bash
docker compose up -d --build backend frontend
docker compose ps
```

Expected: database, backend and frontend are running and the backend is healthy.

- [ ] **Step 4: Verify the real API privacy contract**

```bash
curl -fsS 'http://127.0.0.1:5000/api/image-assets?assignment=unassigned&page=1&per_page=24' \
  | python -c 'import json,sys; d=json.load(sys.stdin); print({"total": d["total"], "returned": len(d["assets"]), "keys": sorted(d["assets"][0])})'
```

Expected: total equals the current unassigned count, returned is at most 24, and keys match the safe DTO.

- [ ] **Step 5: Perform real-browser acceptance**

Open `http://localhost/` and verify:

1. Header shows `2,419 张待归款图片` and `0 个产品` before real association.
2. “待归款图片” is selected by default and the first page has at most 24 cards.
3. Cards render private previews and Chinese/space/multilevel paths; no OSS key appears in the DOM.
4. Search `手机挂绳/A47/修改后` and verify matching cards only.
5. Clear search, navigate to page 2, and verify the page changes.
6. With zero products, selecting a card leaves “关联型号” disabled with guidance to add/import a product.
7. Switching to “产品资料” preserves the existing empty table and add/CSV actions.

Capture and inspect one labeled screenshot of the default asset view for overflow, broken previews and spacing. Do not create a product or mutate the real 2,419-asset dataset during browser acceptance.

- [ ] **Step 6: Run final hygiene checks**

```bash
git diff --check
git status --short
rg -n "oss_path|preview_oss_path|source_bucket|content_hash" frontend/src/components/UnassignedAssetGrid.tsx frontend/src/components/ProductUpload.tsx
```

Expected: no whitespace errors; privacy search has no matches; only task files are changed. Preserve pre-existing `AGENTS.md`, `backend/reports/`, `combo_report_static.md`, `docs/agents/`, and the Issue #12 plan.

- [ ] **Step 7: Commit verification-only fixes when needed**

If browser acceptance finds a frontend defect, first add the failing regression test beside the affected component, then fix it and stage only these explicit candidate files that actually changed:

```bash
git add frontend/src/components/ProductUpload.tsx frontend/src/components/ProductUpload.test.tsx frontend/src/components/UnassignedAssetGrid.tsx frontend/src/components/UnassignedAssetGrid.test.tsx frontend/src/index.css
git commit -m "fix(frontend): refine image asset management"
```

If no adjustment is needed, do not create an empty commit.
