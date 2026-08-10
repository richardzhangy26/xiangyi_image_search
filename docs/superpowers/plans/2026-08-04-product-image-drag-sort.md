# 产品图片拖拽排序 + 第一张即主图 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 产品编辑弹窗中的图片支持拖拽排序，顺序持久化到 `image_assets.sort_order`，第一张即主图（`is_primary`）。

**Architecture:** 在 `image_assets` 表增加 `sort_order` 整数列（每个产品内 0 起递增，0 = 主图）。所有读路径改为按 `(sort_order, created_at, id)` 排序；写路径（创建产品、更新产品、归款）负责维护序号。前端编辑弹窗用 `@dnd-kit` 给 antd `Upload`（picture-card）加拖拽排序，点「确定」时把完整有序列表（既有资产用 asset_id、新文件用 `new:<index>` 占位）随 `PUT /api/products/<model>` 的 `product` JSON 中的 `image_order` 提交，单事务落库；点「取消」排序不生效。

**Tech Stack:** Flask + Flask-SQLAlchemy + PostgreSQL 16/pgvector；React 18 + TypeScript + antd 5 + @dnd-kit/core@^6 + @dnd-kit/sortable@^8 + @dnd-kit/utilities@^3；pytest（集成测试走 Docker PostgreSQL localhost:5433 的 image_search_test 库）+ vitest。

## Global Constraints

- **不在本计划内执行任何 git commit/push**：工作区已存在大量与本特性无关的未提交改动，必须原样保护；全部改动留在工作区，完成后由用户决定如何集成。每个 Task 末尾的"验证"步骤替代模板中的 commit 步骤。
- 只修改本计划列出的文件；需要扩大范围时先报告原因。
- 迁移脚本必须幂等、只能由人工显式运行；**不得**在应用启动、部署或健康检查中隐式执行（AGENTS.md 硬约束）。
- 不触碰 OSS/Kodo 对象；排序只改数据库列，不涉及对象存储写。
- 测试只运行本计划列出的定向命令；禁止运行 `test/test.py`（真实 OSS）、`test/test_pgvector.py`、`test/benchmark_search.py`（手工脚本）。
- `is_primary` 仍然是读路径派生值（排序后枚举 index == 0），不新增布尔列。
- 后端错误响应沿用现有 `error_response(message, error_code, status_code)` 模式，新错误码：`INVALID_IMAGE_ORDER`（400）。

## 架构审查备忘（替代 architect 子代理的设计自检）

- **不变量**：同一产品下 active 资产的 `sort_order` 从 0 连续递增；主图 = sort_order 最小者。重排帮助函数 `apply_product_image_order` 每次全量重写 0..n，天然消除空洞与并列。
- **失败模式**：① 旧库所有行 sort_order 默认为 0，读路径用 `(sort_order, created_at, id)` 排序后行为与现状（created_at 升序）完全一致 → 未跑迁移的旧库行为不变；② 前端占位符 `new:<i>` 越界或资产 id 不存在/属于其他产品 → 跳过该条目而不是报错（弹窗打开期间资产可能被归档，报错会让用户无法保存）；③ `image_order` 类型非法 → 400，不改动任何数据；④ PUT 内排序与新图入库在同一事务，失败整体回滚。
- **回滚**：代码回滚后旧代码不读 sort_order，行为退回 created_at 排序；列保留无害。数据库层面无需回滚脚本。
- **测试接缝**：集成测试 conftest 用 `db.metadata.create_all` 从模型建 schema，模型加列即生效，无需迁移脚本参与测试。

---

### Task 1: 后端 schema —— sort_order 列 + 幂等迁移脚本

**Files:**
- Modify: `backend/models/image_asset.py`
- Modify: `postgres/init/01_init.sql`
- Create: `backend/scripts/migrate_image_asset_sort_order.py`

**Interfaces:**
- Produces: `ImageAsset.sort_order: int`（`nullable=False, default=0`）；`ImageAsset.to_dict()` 新增键 `'sort_order'`；迁移命令 `python -m scripts.migrate_image_asset_sort_order`（幂等，列已存在时跳过回填）。

- [ ] **Step 1: 修改模型** `backend/models/image_asset.py`

在 `status = db.Column(...)` 一行之前插入：

```python
    sort_order = db.Column(
        db.Integer,
        nullable=False,
        default=0,
        comment='商品内图片展示顺序；0 即主图，未归款资产无意义',
    )
```

在 `to_dict` 返回 dict 的 `'status': self.status,` 一行之后插入：

```python
            'sort_order': self.sort_order,
```

- [ ] **Step 2: 同步 Docker 首启 SQL** `postgres/init/01_init.sql`

在 `image_assets` 建表语句的 `status VARCHAR(20) NOT NULL DEFAULT 'active',` 一行之前插入：

```sql
    sort_order            INTEGER NOT NULL DEFAULT 0,
```

在 `COMMENT ON TABLE image_assets ...` 一行之后追加：

```sql
COMMENT ON COLUMN image_assets.sort_order IS '商品内图片展示顺序；0 即主图，未归款资产无意义';
```

- [ ] **Step 3: 编写幂等迁移脚本** `backend/scripts/migrate_image_asset_sort_order.py`

```python
"""为 image_assets 增加 sort_order 列并按 created_at 回填存量数据。

幂等：列已存在时直接跳过，不会覆盖用户已调整过的顺序。
运行方式（backend 目录）：python -m scripts.migrate_image_asset_sort_order
"""

from sqlalchemy import text

from app import create_app
from models import db

_COLUMN_CHECK_SQL = text(
    "SELECT 1 FROM information_schema.columns "
    "WHERE table_name = 'image_assets' AND column_name = 'sort_order'"
)

_ADD_COLUMN_SQL = text(
    "ALTER TABLE image_assets "
    "ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
)

_BACKFILL_SQL = text(
    """
    WITH ranked AS (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY model_number
                   ORDER BY created_at, id
               ) - 1 AS new_order
        FROM image_assets
        WHERE model_number IS NOT NULL
    )
    UPDATE image_assets
    SET sort_order = ranked.new_order
    FROM ranked
    WHERE image_assets.id = ranked.id
    """
)


def migrate_image_asset_sort_order():
    """返回 True 表示执行了迁移，False 表示列已存在无需处理。"""
    with db.session.begin():
        if db.session.execute(_COLUMN_CHECK_SQL).scalar():
            return False
        db.session.execute(_ADD_COLUMN_SQL)
        db.session.execute(_BACKFILL_SQL)
    return True


def main():
    app = create_app()
    with app.app_context():
        if migrate_image_asset_sort_order():
            print('已添加 image_assets.sort_order 并按创建时间回填存量顺序。')
        else:
            print('image_assets.sort_order 已存在，跳过迁移（未改动任何数据）。')


if __name__ == '__main__':
    main()
```

- [ ] **Step 4: 验证模型与现有测试兼容**

Run: `cd /Users/zhangyichi/github/xiangyi_image_search/backend && python -m pytest test/integration/test_write_paths.py -v`
Expected: 全部 PASS（新列有默认值，现有行为不变；集成库不可达时自动 skip，也算通过）

---

### Task 2: 后端读写路径 —— 排序查询 + apply 帮助函数 + POST/PUT/assign

**Files:**
- Modify: `backend/blueprints/products_v2.py`
- Modify: `backend/blueprints/image_assets.py`
- Test: `backend/test/integration/test_write_paths.py`

**Interfaces:**
- Consumes: Task 1 的 `ImageAsset.sort_order`。
- Produces:
  - `apply_product_image_order(model_number: str, ordered_asset_ids: list[str]) -> None`（products_v2.py 内部函数）
  - `PUT /api/products/<model_number>` 的 `product` JSON 接受可选 `image_order: string[]`；条目为既有资产 asset_id 或 `"new:<index>"`（index 对应本次 multipart `images` 文件顺序，0 起）。非法类型返回 `400 INVALID_IMAGE_ORDER`。
  - `POST /api/image-assets/assign`：新关联资产追加到目标产品队尾。

- [ ] **Step 1: 先写失败测试**（追加到 `backend/test/integration/test_write_paths.py` 末尾）

```python
def _upload_product_with_colors(client, model_number, colors):
    files = [
        (io.BytesIO(_png_bytes(color)), f'{color}.png') for color in colors
    ]
    return client.post('/api/products', data={
        'product': _product_payload(model_number),
        'images': files,
    }, content_type='multipart/form-data')


def _image_names(images):
    return [
        image['source_relative_path'].rsplit('/', 1)[-1] for image in images
    ]


def test_create_product_persists_upload_order_with_first_as_primary(app):
    _install_asset_dependencies(app)
    client = app.test_client()
    created = _upload_product_with_colors(client, 'ORDER-001', ['red', 'green', 'blue'])
    assert created.status_code == 201

    images = client.get('/api/products/ORDER-001').get_json()['images']
    assert _image_names(images) == ['red.png', 'green.png', 'blue.png']
    assert [image['image_order'] for image in images] == [0, 1, 2]
    assert [image['is_primary'] for image in images] == [True, False, False]
    sort_orders = {
        asset.source_relative_path.rsplit('/', 1)[-1]: asset.sort_order
        for asset in ImageAsset.query.all()
    }
    assert sort_orders == {'red.png': 0, 'green.png': 1, 'blue.png': 2}


def test_update_product_applies_explicit_image_order(app):
    _install_asset_dependencies(app)
    client = app.test_client()
    assert _upload_product_with_colors(
        client, 'ORDER-002', ['red', 'green', 'blue']
    ).status_code == 201
    before = client.get('/api/products/ORDER-002').get_json()['images']
    reordered = [before[2]['asset_id'], before[0]['asset_id'], before[1]['asset_id']]

    response = client.put('/api/products/ORDER-002', data={
        'product': json.dumps({'image_order': reordered}),
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    after = client.get('/api/products/ORDER-002').get_json()['images']
    assert [image['asset_id'] for image in after] == reordered
    assert _image_names(after) == ['blue.png', 'red.png', 'green.png']
    assert [image['is_primary'] for image in after] == [True, False, False]


def test_update_product_image_order_places_new_upload_at_placeholder(app):
    _install_asset_dependencies(app)
    client = app.test_client()
    assert _upload_product_with_colors(
        client, 'ORDER-003', ['red', 'blue']
    ).status_code == 201
    before = client.get('/api/products/ORDER-003').get_json()['images']

    response = client.put('/api/products/ORDER-003', data={
        'product': json.dumps({'image_order': [
            before[0]['asset_id'], 'new:0', before[1]['asset_id'],
        ]}),
        'images': [(io.BytesIO(_png_bytes('green')), 'green.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    after = client.get('/api/products/ORDER-003').get_json()['images']
    assert _image_names(after) == ['red.png', 'green.png', 'blue.png']


def test_update_product_appends_new_uploads_without_image_order(app):
    _install_asset_dependencies(app)
    client = app.test_client()
    assert _upload_product_with_colors(
        client, 'ORDER-004', ['red', 'green']
    ).status_code == 201

    response = client.put('/api/products/ORDER-004', data={
        'product': json.dumps({'photographer_file': 'changed'}),
        'images': [(io.BytesIO(_png_bytes('blue')), 'blue.png')],
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    after = client.get('/api/products/ORDER-004').get_json()['images']
    assert _image_names(after) == ['red.png', 'green.png', 'blue.png']


def test_update_product_rejects_invalid_image_order(app):
    _install_asset_dependencies(app)
    client = app.test_client()
    assert _upload_product_with_colors(client, 'ORDER-005', ['red']).status_code == 201

    for bad_payload in ('not-a-list', [123]):
        response = client.put('/api/products/ORDER-005', data={
            'product': json.dumps({'image_order': bad_payload}),
        }, content_type='multipart/form-data')
        assert response.status_code == 400
        assert response.get_json()['error_code'] == 'INVALID_IMAGE_ORDER'
    images = client.get('/api/products/ORDER-005').get_json()['images']
    assert _image_names(images) == ['red.png']


def test_update_product_image_order_skips_unknown_and_foreign_assets(app):
    _install_asset_dependencies(app)
    client = app.test_client()
    assert _upload_product_with_colors(
        client, 'ORDER-006', ['red', 'green']
    ).status_code == 201
    assert _upload_product_with_colors(
        client, 'ORDER-007', ['blue']
    ).status_code == 201
    own = client.get('/api/products/ORDER-006').get_json()['images']
    foreign = client.get('/api/products/ORDER-007').get_json()['images'][0]

    response = client.put('/api/products/ORDER-006', data={
        'product': json.dumps({'image_order': [
            own[1]['asset_id'],
            '00000000-0000-0000-0000-000000000000',
            foreign['asset_id'],
        ]}),
    }, content_type='multipart/form-data')

    assert response.status_code == 200
    after = client.get('/api/products/ORDER-006').get_json()['images']
    assert _image_names(after) == ['green.png', 'red.png']
    assert _image_names(
        client.get('/api/products/ORDER-007').get_json()['images']
    ) == ['blue.png']


def test_assign_appends_assets_after_existing_images(app):
    _install_asset_dependencies(app)
    client = app.test_client()
    assert _upload_product_with_colors(client, 'ASSIGN-001', ['red']).status_code == 201
    primary_id = client.get('/api/products/ASSIGN-001').get_json()['images'][0]['asset_id']
    assert _upload_product_with_colors(
        client, 'ASSIGN-TMP', ['green', 'blue']
    ).status_code == 201
    tmp_images = client.get('/api/products/ASSIGN-TMP').get_json()['images']
    tmp_ids = [image['asset_id'] for image in tmp_images]
    assert client.delete('/api/products/ASSIGN-TMP').status_code == 200

    response = client.post('/api/image-assets/assign', json={
        'asset_ids': tmp_ids,
        'model_number': 'ASSIGN-001',
    })

    assert response.status_code == 200
    images = client.get('/api/products/ASSIGN-001').get_json()['images']
    assert [image['asset_id'] for image in images] == [primary_id] + tmp_ids
    assert [image['is_primary'] for image in images] == [True, False, False]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/zhangyichi/github/xiangyi_image_search/backend && python -m pytest test/integration/test_write_paths.py -v`
Expected: 新测试 FAIL（排序未持久化 / image_order 被忽略 / error_code 不存在），旧测试 PASS

- [ ] **Step 3: 修改读路径排序** `backend/blueprints/products_v2.py` 的 `products_with_active_images`

把 `.order_by(` 块改为：

```python
    ).order_by(
        ImageAsset.model_number,
        ImageAsset.sort_order,
        ImageAsset.created_at,
        ImageAsset.id,
    ).all()
```

- [ ] **Step 4: 新增 apply 帮助函数**（放在 `image_asset_for_product` 之后）

```python
def apply_product_image_order(model_number, ordered_asset_ids):
    """按给定顺序重写商品活动资产的 sort_order（0 起连续）。

    未出现在列表中的本商品资产按当前顺序追加到队尾；列表中不存在、
    不属于本商品或重复的条目会被跳过。
    """
    assets = ImageAsset.query.filter(
        ImageAsset.model_number == model_number,
        ImageAsset.status == 'active',
    ).order_by(
        ImageAsset.sort_order,
        ImageAsset.created_at,
        ImageAsset.id,
    ).all()
    assets_by_id = {str(asset.id): asset for asset in assets}
    position = 0
    seen = set()
    for asset_id in ordered_asset_ids:
        if asset_id in seen:
            continue
        asset = assets_by_id.get(asset_id)
        if asset is None:
            continue
        asset.sort_order = position
        seen.add(asset_id)
        position += 1
    for asset in assets:
        if str(asset.id) in seen:
            continue
        asset.sort_order = position
        position += 1
```

- [ ] **Step 5: 接线 create_product**

在 `create_product` 中，把 ingest 循环改为同时收集资产 id：

```python
        image_results = []
        ingested_asset_ids = []
        request_id = uuid.uuid4().hex

        if relative_paths:
            ingest_service = get_asset_ingest_service(source)
            for relative_path in relative_paths:
                result = ingest_service.ingest_one(
                    relative_path,
                    model_number=model_number,
                    request_id=request_id,
                    commit=False,
                )
                image_result = attach_product_upload_result(
                    result,
                    model_number,
                )
                if image_result:
                    image_results.append(image_result)
                    ingested_asset_ids.append(str(result.asset_id))

        if ingested_asset_ids:
            apply_product_image_order(model_number, ingested_asset_ids)
```

- [ ] **Step 6: 接线 update_product（含 image_order 解析）**

把 `update_product` 中「获取更新数据」到 `db.session.commit()` 之间的主体替换为：

```python
        # 获取更新数据
        image_order_raw = None
        if product_data_str := request.form.get('product'):
            product_data = json.loads(product_data_str)
            image_order_raw = product_data.get('image_order')

            # 更新字段（排除主键）
            for key, value in product_data.items():
                if key != 'model_number' and hasattr(product, key):
                    setattr(product, key, value)

        if image_order_raw is not None and (
            not isinstance(image_order_raw, list)
            or any(not isinstance(entry, str) for entry in image_order_raw)
        ):
            db.session.rollback()
            return error_response('图片排序参数无效', 'INVALID_IMAGE_ORDER', 400)

        existing_ordered_ids = [
            str(asset.id)
            for asset in ImageAsset.query.filter(
                ImageAsset.model_number == model_number,
                ImageAsset.status == 'active',
            ).order_by(
                ImageAsset.sort_order,
                ImageAsset.created_at,
                ImageAsset.id,
            ).all()
        ]

        source, relative_paths = prepare_product_uploads(
            request.files.getlist('images'),
            model_number,
        )
        image_results = []
        ingested_asset_ids = []
        request_id = uuid.uuid4().hex

        if relative_paths:
            ingest_service = get_asset_ingest_service(source)
            for relative_path in relative_paths:
                result = ingest_service.ingest_one(
                    relative_path,
                    model_number=model_number,
                    request_id=request_id,
                    commit=False,
                )
                image_result = attach_product_upload_result(
                    result,
                    model_number,
                )
                if image_result:
                    image_results.append(image_result)
                    ingested_asset_ids.append(str(result.asset_id))

        if image_order_raw is not None:
            resolved_order = []
            for entry in image_order_raw:
                if entry.startswith('new:'):
                    index_text = entry[4:]
                    if index_text.isdigit():
                        index = int(index_text)
                        if index < len(ingested_asset_ids):
                            resolved_order.append(ingested_asset_ids[index])
                    continue
                resolved_order.append(entry)
            apply_product_image_order(model_number, resolved_order)
        elif ingested_asset_ids:
            apply_product_image_order(
                model_number,
                existing_ordered_ids + ingested_asset_ids,
            )

        db.session.commit()
```

注意：`INVALID_IMAGE_ORDER` 分支的显式 `db.session.rollback()` 用于撤销字段 setattr 的脏状态；该 400 也要能被现有的通用 `except Exception` 之外的正常路径返回（它在 try 块内直接 return）。

- [ ] **Step 7: 归款追加队尾** `backend/blueprints/image_assets.py`

文件顶部导入区加 `from sqlalchemy import func`（与现有 `from sqlalchemy.exc import IntegrityError` 并列）。

在 `assign_image_assets` 的 `assets = ImageAsset.query...` 锁定查询之后、赋值循环之前，插入：

```python
    next_order = db.session.query(
        func.coalesce(func.max(ImageAsset.sort_order), -1)
    ).filter(
        ImageAsset.model_number == model_number,
        ImageAsset.status == 'active',
    ).scalar() + 1
```

把赋值循环替换为（按请求顺序分配队尾序号，复用已有资产保持原顺序）：

```python
    assets_by_id = {asset.id: asset for asset in assets}
    assigned_count = 0
    reused_count = 0
    for requested_id in asset_ids:
        asset = assets_by_id[requested_id]
        if asset.model_number == model_number:
            reused_count += 1
        else:
            asset.model_number = model_number
            asset.sort_order = next_order
            next_order += 1
            assigned_count += 1
```

- [ ] **Step 8: 运行全部定向后端测试**

Run: `cd /Users/zhangyichi/github/xiangyi_image_search/backend && python -m pytest test/integration/test_write_paths.py test/integration/test_image_asset_management.py -v`
Expected: 全部 PASS（含新旧用例）

---

### Task 3: 前端依赖 + 排序负载纯函数

**Files:**
- Modify: `frontend/package.json`（+ 重新生成 `package-lock.json` 与 `yarn.lock`）
- Create: `frontend/src/components/productImageOrder.ts`
- Test: `frontend/src/components/productImageOrder.test.ts`

**Interfaces:**
- Produces:
  - `buildImageOrderPayload(fileList: UploadFile[]): { imageFiles: File[]; imageOrder: string[] }` — 既有资产（无 `originFileObj`）用 `uid`（= asset_id），新文件用 `new:<index>` 占位。
  - `moveByUid(list: UploadFile[], activeUid: string, overUid: string): UploadFile[]` — 拖拽落点重排，找不到或相同则原样返回。

- [ ] **Step 1: 安装依赖并同步两份 lock**

```bash
cd /Users/zhangyichi/github/xiangyi_image_search/frontend
yarn add @dnd-kit/core@^6 @dnd-kit/sortable@^8 @dnd-kit/utilities@^3
npm install --package-lock-only
```

（yarn 更新 package.json + yarn.lock；npm --package-lock-only 让 Docker 构建用的 package-lock.json 同步，不动 node_modules。）

- [ ] **Step 2: 先写失败测试** `frontend/src/components/productImageOrder.test.ts`

```ts
import { describe, expect, it } from 'vitest';
import type { UploadFile } from 'antd/es/upload/interface';
import { buildImageOrderPayload, moveByUid } from './productImageOrder';

const existing = (uid: string): UploadFile => ({
  uid,
  name: `${uid}.jpg`,
  status: 'done',
});

const fresh = (uid: string): UploadFile => ({
  uid,
  name: `${uid}.jpg`,
  status: 'done',
  originFileObj: new File(['x'], `${uid}.jpg`, { type: 'image/jpeg' }),
});

describe('buildImageOrderPayload', () => {
  it('keeps existing asset ids and places new uploads as new:<index>', () => {
    const { imageFiles, imageOrder } = buildImageOrderPayload([
      existing('asset-a'),
      fresh('rc-1'),
      existing('asset-b'),
      fresh('rc-2'),
    ]);
    expect(imageOrder).toEqual(['asset-a', 'new:0', 'asset-b', 'new:1']);
    expect(imageFiles.map((file) => file.name)).toEqual(['rc-1.jpg', 'rc-2.jpg']);
  });

  it('returns empty payload for empty list', () => {
    expect(buildImageOrderPayload([])).toEqual({ imageFiles: [], imageOrder: [] });
  });
});

describe('moveByUid', () => {
  it('moves the dragged item before the drop target order-wise', () => {
    const result = moveByUid(
      [existing('a'), existing('b'), existing('c')],
      'c',
      'a'
    );
    expect(result.map((item) => item.uid)).toEqual(['c', 'a', 'b']);
  });

  it('returns the original list when uid is missing or unchanged', () => {
    const list = [existing('a'), existing('b')];
    expect(moveByUid(list, 'a', 'zzz')).toBe(list);
    expect(moveByUid(list, 'a', 'a')).toBe(list);
  });
});
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd /Users/zhangyichi/github/xiangyi_image_search/frontend && npx vitest run src/components/productImageOrder.test.ts`
Expected: FAIL（模块不存在）

- [ ] **Step 4: 实现纯函数** `frontend/src/components/productImageOrder.ts`

```ts
import type { UploadFile } from 'antd/es/upload/interface';

/**
 * 把编辑弹窗的图片列表转换为后端排序负载：
 * 既有资产使用 asset_id（即 UploadFile.uid），新上传文件使用 new:<index> 占位，
 * index 对应 imageFiles 数组下标（与 multipart images 字段顺序一致）。
 */
export function buildImageOrderPayload(fileList: UploadFile[]): {
  imageFiles: File[];
  imageOrder: string[];
} {
  const imageFiles: File[] = [];
  const imageOrder: string[] = [];
  fileList.forEach((file) => {
    if (file.originFileObj) {
      imageOrder.push(`new:${imageFiles.length}`);
      imageFiles.push(file.originFileObj as File);
    } else {
      imageOrder.push(file.uid);
    }
  });
  return { imageFiles, imageOrder };
}

/** 拖拽结束后按 uid 重排，返回新数组；找不到或位置不变时原样返回。 */
export function moveByUid(
  list: UploadFile[],
  activeUid: string,
  overUid: string
): UploadFile[] {
  const from = list.findIndex((item) => item.uid === activeUid);
  const to = list.findIndex((item) => item.uid === overUid);
  if (from < 0 || to < 0 || from === to) {
    return list;
  }
  const next = [...list];
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  return next;
}
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd /Users/zhangyichi/github/xiangyi_image_search/frontend && npx vitest run src/components/productImageOrder.test.ts`
Expected: PASS

---

### Task 4: 前端弹窗接入拖拽排序 + 主图角标 + 提交负载

**Files:**
- Modify: `frontend/src/components/ProductUpload.tsx`
- Modify: `frontend/src/services/productApi.ts`

**Interfaces:**
- Consumes: Task 3 的 `buildImageOrderPayload` / `moveByUid`；Task 2 的 `image_order` API 契约。
- Produces: `updateProduct(modelNumber, productData: Partial<ProductFormData> & { image_order?: string[] }, newImages?)`。

- [ ] **Step 1: 放宽 updateProduct 参数类型** `frontend/src/services/productApi.ts`

把 `updateProduct` 的签名改为：

```ts
export const updateProduct = async (
  modelNumber: string,
  productData: Partial<ProductFormData> & { image_order?: string[] },
  newImages?: File[]
): Promise<ProductImageWriteSummary & { message: string }> => {
```

- [ ] **Step 2: ProductUpload.tsx 顶部新增导入**

```tsx
import { DndContext, PointerSensor, closestCenter, useSensor, useSensors } from '@dnd-kit/core';
import type { DragEndEvent } from '@dnd-kit/core';
import { SortableContext, rectSortingStrategy, useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
```

以及本地帮助函数：

```tsx
import { buildImageOrderPayload, moveByUid } from './productImageOrder';
```

- [ ] **Step 3: 新增可拖拽卡片组件**（放在 `ProductUpload` 组件定义之前，模块级）

```tsx
interface SortableUploadItemProps {
  file: UploadFile;
  isPrimary: boolean;
  children: React.ReactNode;
}

const SortableUploadItem: React.FC<SortableUploadItemProps> = ({
  file,
  isPrimary,
  children,
}) => {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: file.uid });

  return (
    <div
      ref={setNodeRef}
      style={{
        transform: CSS.Translate.toString(transform),
        transition,
        position: 'relative',
        zIndex: isDragging ? 20 : undefined,
        cursor: 'grab',
      }}
      {...attributes}
      {...listeners}
    >
      {isPrimary && (
        <span className="absolute top-1 left-1 z-10 px-1.5 py-0.5 rounded text-xs font-medium bg-teal-600/90 text-white shadow-sm pointer-events-none">
          主图
        </span>
      )}
      {children}
    </div>
  );
};
```

- [ ] **Step 4: 组件内新增 sensors 与拖拽处理**（放在 `const [form] = Form.useForm();` 之后）

```tsx
  const imageSensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } })
  );
  const watchedImages: UploadFile[] = Form.useWatch('images', form) || [];

  const handleImageDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return;
    const current =
      (form.getFieldValue('images') as UploadFile[] | undefined) || [];
    form.setFieldsValue({
      images: moveByUid(current, String(active.id), String(over.id)),
    });
  };
```

（`distance: 8` 激活阈值保证点击预览/删除按钮不会误触发拖拽。）

- [ ] **Step 5: 改造图片上传 Form.Item**（弹窗内 `{/* 图片上传 */}` 处）

```tsx
          <Form.Item
            name="images"
            label="产品图片"
            extra="拖拽调整顺序，第一张为主图"
            valuePropName="fileList"
            getValueFromEvent={(e) => {
              if (Array.isArray(e)) {
                return e;
              }
              return e?.fileList;
            }}
          >
            <DndContext
              sensors={imageSensors}
              collisionDetection={closestCenter}
              onDragEnd={handleImageDragEnd}
            >
              <SortableContext
                items={watchedImages.map((file) => file.uid)}
                strategy={rectSortingStrategy}
              >
                <Upload
                  listType="picture-card"
                  multiple
                  beforeUpload={() => false}
                  accept="image/*"
                  itemRender={(originNode, file, fileList) => (
                    <SortableUploadItem
                      file={file}
                      isPrimary={fileList[0]?.uid === file.uid}
                    >
                      {originNode}
                    </SortableUploadItem>
                  )}
                >
                  <div>
                    <PlusOutlined />
                    <div style={{ marginTop: 8 }}>上传图片</div>
                  </div>
                </Upload>
              </SortableContext>
            </DndContext>
          </Form.Item>
```

- [ ] **Step 6: handleAddEdit 改用排序负载**

把 `handleAddEdit` 中「处理图片文件」段落替换为：

```tsx
        // 处理图片文件与排序负载（既有资产用 asset_id，新文件用 new:<index>）
        const fileList = (values.images as UploadFile[] | undefined) || [];
        const { imageFiles, imageOrder } = buildImageOrderPayload(fileList);

        const { images, ...productData } = values;

        if (editingProduct) {
          const retainedAssetIds = new Set(
            fileList.map((file) => file.uid)
          );
```

后续编辑分支的 `updateProduct` 调用改为：

```tsx
          await updateProduct(
            editingProduct.model_number,
            { ...productData, image_order: imageOrder },
            imageFiles
          );
```

创建分支保持 `createProduct(productData as ProductFormData, imageFiles)` 不变（后端按上传顺序赋 0..n）。

- [ ] **Step 7: 前端验证**

Run: `cd /Users/zhangyichi/github/xiangyi_image_search/frontend && npx vitest run`
Expected: 全部 PASS（含既有 ProductUpload.test.tsx / UnassignedAssetGrid.test.tsx）

Run: `cd /Users/zhangyichi/github/xiangyi_image_search/frontend && npm run build`
Expected: tsc + vite build 成功，无类型错误

---

### Task 5: 文档同步 + 存量库迁移（人工确认）+ 收尾验证

**Files:**
- Modify: `AGENTS.md`
- Modify（如其中记录了 image_assets 列清单）: `backend/README_DATABASE.md`、`backend/ARCHITECTURE.md` —— 先检查再决定是否改

**Interfaces:**
- Consumes: Task 1-4 全部产物。

- [ ] **Step 1: 更新 AGENTS.md 数据库结构一节**

在 `### image_assets` 字段表的 `normalization_version / status` 一行之前插入：

```
| sort_order | 商品内图片展示顺序；0 即主图，未归款资产无意义（默认 0） |
```

并在该表下方的索引说明之后追加一行事实：

```
产品图片接口按 (sort_order, created_at, id) 升序返回，is_primary 由排序后首位派生；
新上传与归款图片追加队尾，编辑弹窗拖拽排序随 PUT /api/products/<model> 的 image_order 单事务保存。
存量数据库通过 python -m scripts.migrate_image_asset_sort_order（幂等）补列并回填，不在应用启动时隐式执行。
```

- [ ] **Step 2: 检查并同步其他文档**

Run: `cd /Users/zhangyichi/github/xiangyi_image_search && grep -n "image_assets" backend/README_DATABASE.md backend/ARCHITECTURE.md | head -40`
若文件含 image_assets 列清单则补 `sort_order`；不含则不改。

- [ ] **Step 3: 对本地开发库执行迁移（需用户当场确认后执行）**

```bash
cd /Users/zhangyichi/github/xiangyi_image_search/backend && python -m scripts.migrate_image_asset_sort_order
```

预期输出：`已添加 image_assets.sort_order 并按创建时间回填存量顺序。`
再执行一次验证幂等，预期输出：`image_assets.sort_order 已存在，跳过迁移（未改动任何数据）。`

- [ ] **Step 4: 全量定向回归**

Run: `cd /Users/zhangyichi/github/xiangyi_image_search/backend && python -m pytest test/ --ignore=test/integration --ignore=test/test.py --ignore=test/test_pgvector.py --ignore=test/benchmark_search.py -v && python -m pytest test/integration/ -v`
Expected: 全部 PASS（不可达集成库自动 skip 除外）

- [ ] **Step 5: 独立风险审查**

对完整 diff（`git diff` + 新增文件）发起独立 code review 子代理，重点核对：事务边界（image_order 应用与 ingest 同事务）、归款并发赋值、旧库全 0 sort_order 的兼容行为、错误码稳定性、前端表单状态一致性。审查发现的问题修复后重新回归。

---

## Self-Review 记录

- **Spec 覆盖**：拖拽排序（Task 3/4）、第一张即主图（Task 2 读路径派生 + Task 4 角标）、顺序持久化（Task 1/2）、新图/归款追加队尾（Task 2）、随弹窗保存（Task 4 + Task 2 PUT 契约）、存量回填（Task 1/5）、文档（Task 5）。无缺口。
- **Placeholder 扫描**：无 TBD/TODO；所有代码步骤含完整代码。
- **类型一致性**：`image_order`（API 字段，snake_case）↔ `imageOrder`（前端变量，camelCase）在 Task 4 Step 6 显式映射；`new:<index>` 占位格式在 Task 2/3/4 三处一致；`apply_product_image_order(model_number, ordered_asset_ids)` 签名在定义与三个调用点一致。
