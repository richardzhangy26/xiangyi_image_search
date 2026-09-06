# Issue #15 Asset Display Name Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task in the current delegated worktree. Do not commit because the task owner explicitly prohibited commits.

**Goal:** 为活跃图片资产增加可编辑显示名称、原子乐观并发改名、活动记录与显示名称/来源路径双字段搜索，并在产品管理顶层流程覆盖已归款和未归款资产。

**Architecture:** PostgreSQL 通过显式幂等迁移增加 `display_name`、`version` 与通用活动记录表；专用命名服务执行校验和单条条件 UPDATE，蓝图只映射 HTTP。前端使用共享卡片编辑器，由通用资产工作台承载 unassigned/assigned/all 三种列表；向量检索与产品图片只扩展安全表示，不改变检索或对象存储流程。

**Tech Stack:** Python 3.9、Flask、Flask-SQLAlchemy、PostgreSQL 16、pgvector、pytest、React 18、TypeScript、Ant Design 5、Vitest、React Testing Library。

## Global Constraints

- 基线固定为 `refactor/image-search-pgvector@088bb9f`；只修改当前 Codex worktree。
- 不运行正式数据库迁移、真实 OSS/Kodo、DashScope、部署、删除、手工 pgvector benchmark 或任何云端写入。
- 不加载 DB 凭证、不连接 `image_search_test` 或任何 PostgreSQL、不执行迁移/集成测试；真实 PostgreSQL 测试只编写并列为待授权。
- 实际执行范围只包括纯单元、Mock session、静态合同和前端 fake-API 测试；外部适配器一律伪造。
- 不修改资产 ID、`source_relative_path`、OSS key、预览、向量、embedding 或归款关系。
- `display_name` 允许重名；归档资产拒绝改名；扩展名由服务端从来源路径决定。
- 名称更新与成功活动记录必须同事务；陈旧版本必须返回 409 与最新安全表示。
- 应用启动、健康检查和普通请求不得隐式执行迁移。
- 不 commit、push、部署、创建/修改 Issue 或 PR。

## File Map

| 文件 | 职责 |
|---|---|
| `backend/services/asset_display_name.py` | basename、扩展名、名称主体校验、条件更新与活动记录事务。 |
| `backend/models/image_asset.py` | `display_name`、`version` 与稳定资产表示。 |
| `backend/models/asset_activity_record.py` | 无级联外键的通用活动记录 ORM。 |
| `backend/migrations/issue_15_asset_display_name.py` | 显式、幂等、可导入测试的 PostgreSQL 迁移。 |
| `backend/init_db.py` | 显式建库时复用旧应用 INSERT 兼容触发器 SQL。 |
| `backend/blueprints/image_assets.py` | 管理列表 DTO、双字段搜索和 rename HTTP 命令。 |
| `backend/services/vector_search.py` | 向量结果增加显示名称、来源路径和版本。 |
| `backend/blueprints/products_v2.py` | 产品图片表示增加新字段。 |
| `postgres/init/01_init.sql` | 新库 schema、触发器和活动记录表。 |
| `frontend/src/components/AssetDisplayNameEditor.tsx` | 卡片内显式改名状态机。 |
| `frontend/src/components/UnassignedAssetGrid.tsx` | 通用活跃资产卡片与归款筛选 UI。 |
| `frontend/src/components/ProductUpload.tsx` | 顶层筛选、分页、列表刷新和改名结果编排。 |
| `frontend/src/components/ProductSearch.tsx` | 搜索结果显示名称为主、来源路径为次。 |
| `frontend/src/services/productApi.ts` | assignment 查询与结构化 rename API 错误。 |
| `frontend/src/types/product.ts` | 统一资产表示、版本和 rename 类型。 |
| `backend/test/test_asset_display_name_unit.py` | 不连接数据库的名称校验与 Mock session 事务分支。 |
| `backend/test/test_issue_15_static_contract.py` | 不连接数据库的迁移、schema、路由与表示静态合同。 |

---

### Task 1: 先用纯单元/静态合同固定迁移与模型设计

**Files:**
- Create: `backend/models/asset_activity_record.py`
- Create: `backend/migrations/__init__.py`
- Create: `backend/migrations/issue_15_asset_display_name.py`
- Modify: `backend/models/image_asset.py`
- Modify: `backend/models/__init__.py`
- Modify: `backend/services/asset_ingest.py`
- Modify: `postgres/init/01_init.sql`
- Modify: `backend/test/integration/test_schema.py`
- Create: `backend/test/integration/test_issue_15_migration.py`
- Modify: `backend/test/integration/test_asset_ingest.py`
- Create: `backend/test/test_asset_display_name_unit.py`
- Create: `backend/test/test_issue_15_static_contract.py`

**Interfaces:**
- Produces: `default_display_name(source_relative_path: str) -> str`。
- Produces: `apply_migration(connection) -> None`，调用方控制连接与事务。
- Produces: `ImageAsset.display_name: str`、`ImageAsset.version: int`。
- Produces: `AssetActivityRecord` ORM；不关联级联 FK。

- [ ] **Step 1: 写迁移失败测试**

在 `test_issue_15_migration.py` 中先用现有模型插入多层、Unicode、空格、多点扩展名资产，再删除新增表/列模拟旧 schema，调用迁移两次：

```python
def test_issue_15_migration_backfills_and_is_idempotent(app):
    asset = _legacy_asset('中文 目录/夏季.蓝色.PNG')
    db.session.add(asset)
    db.session.commit()
    connection = db.session.connection()
    connection.execute(text('DROP TABLE IF EXISTS asset_activity_records'))
    connection.execute(text('ALTER TABLE image_assets DROP COLUMN display_name'))
    connection.execute(text('ALTER TABLE image_assets DROP COLUMN version'))
    connection.commit()

    apply_migration(connection)
    first = connection.execute(text(
        'SELECT display_name, version FROM image_assets WHERE id = :id'
    ), {'id': asset.id}).one()
    assert first == ('夏季.蓝色.PNG', 1)

    connection.execute(text(
        "UPDATE image_assets SET display_name = '人工名称.PNG', version = 7 WHERE id = :id"
    ), {'id': asset.id})
    connection.commit()
    apply_migration(connection)
    second = connection.execute(text(
        'SELECT display_name, version FROM image_assets WHERE id = :id'
    ), {'id': asset.id}).one()
    assert second == ('人工名称.PNG', 7)
```

同文件断言活动表字段/索引和 INSERT 触发器；`test_schema.py` 断言新库两列、版本 check 与活动表；入库测试断言新资产默认 basename 和版本 1。

- [ ] **Step 2: 运行纯单元/静态测试确认因迁移/字段不存在而失败**

Run:

```bash
cd backend
python -m pytest test/test_asset_display_name_unit.py test/test_issue_15_static_contract.py -v
```

Expected: FAIL，明确缺少迁移模块、命名函数、`display_name`、`version`、活动表或路由合同；命令不得尝试连接数据库。

- [ ] **Step 3: 实现最小模型与显式迁移**

`image_asset.py` 使用共享 basename 默认值：

```python
display_name = db.Column(
    db.Text,
    nullable=False,
    default=lambda context: default_display_name(
        context.get_current_parameters()['source_relative_path']
    ),
)
version = db.Column(db.BigInteger, nullable=False, default=1)
```

`asset_activity_record.py` 定义计划 File Map 中的字段，JSON 字段使用 PostgreSQL `JSONB`，索引名固定为 `idx_asset_activity_target_created` 与 `idx_asset_activity_request_id`。

迁移模块暴露 `apply_migration(connection)`，并把无副作用的兼容触发器 SQL 常量提供给显式 `init_db.py` 建库命令复用；应用启动和健康检查均不导入或执行迁移。迁移按规格依次执行：

```sql
ALTER TABLE image_assets ADD COLUMN IF NOT EXISTS display_name TEXT;
ALTER TABLE image_assets ADD COLUMN IF NOT EXISTS version BIGINT;
UPDATE image_assets
SET display_name = regexp_replace(source_relative_path, '^.*/', '')
WHERE display_name IS NULL;
UPDATE image_assets SET version = 1 WHERE version IS NULL;
```

随后创建 `BEFORE INSERT` 触发器、版本 check、非空/默认、活动表和索引。约束通过 `pg_constraint` 条件 DO block 幂等创建。CLI `main()` 必须要求显式 `--apply`；该模块不被 `app.py` 或 `init_db.py` 导入。

`asset_ingest.py` 的 `ImageAsset(...)` 显式传入：

```python
display_name=default_display_name(prepared.source_relative_path),
version=1,
```

- [ ] **Step 4: 运行纯单元/静态测试确认通过**

Run 同 Step 2。所有 integration/PostgreSQL 测试只编写，不执行。

Expected: 所选纯单元/静态测试全部 PASS；输出不得包含数据库连接、OSS 或 DashScope 调用。

- [ ] **Step 5: 检查未提交 diff**

Run:

```bash
git diff --check
git status --short
```

Expected: 仅本 Task 文件与既有规格/计划为未提交变更；无 commit。

---

### Task 2: 用 Mock session 与未执行 API 场景固定校验、OCC 与活动原子性

**Files:**
- Create: `backend/services/asset_display_name.py`
- Modify: `backend/blueprints/image_assets.py`
- Modify: `backend/test/integration/test_image_asset_management.py`
- Modify: `backend/test/test_asset_display_name_unit.py`

**Interfaces:**
- Consumes: `ImageAsset.display_name/version` 与 `AssetActivityRecord`。
- Produces: `normalize_name_body(value: object) -> str`。
- Produces: `rename_asset(asset_id, name_body, expected_version, request_id) -> RenameOutcome`。
- Produces: `POST /api/image-assets/<uuid>/rename`。

- [ ] **Step 1: 写失败的公开 API 场景测试**

在 `test_image_asset_management.py` 增加：

```python
def test_renames_assigned_and_unassigned_assets_with_duplicate_names(app):
    assigned = _asset('已归款/旧名.JPG', model_number='CS-001')
    unassigned = _asset('未归款/旧名.JPG')
    db.session.add_all([_product(), assigned, unassigned])
    db.session.commit()
    client = app.test_client()

    for asset in (assigned, unassigned):
        response = client.post(f'/api/image-assets/{asset.id}/rename', json={
            'name_body': ' 同一个名称 ', 'expected_version': 1,
        })
        assert response.status_code == 200
        assert response.get_json()['asset']['display_name'] == '同一个名称.JPG'
        assert response.get_json()['asset']['version'] == 2
```

再增加参数化校验（空、101 字、`/`、`\\`、控制字符、`.`、`..`）、1/100 字边界、扩展名大小写、归档拒绝、陈旧版本返回 latest，以及 `AssetActivityRecord` 成功行的 before/after/result。

原子性测试 monkeypatch `db.session.flush` 在活动记录加入后抛出异常，断言响应 500 且重新读取资产仍为旧名称/旧版本。配置一个所有方法均抛错的 `IMAGE_ASSET_STORAGE`，成功改名证明未触发它。

- [ ] **Step 2: 运行 Mock session 单元测试确认实现缺失失败**

Run:

```bash
cd backend
python -m pytest test/test_asset_display_name_unit.py -k 'rename or display_name or conflict or archived' -v
```

Expected: FAIL，缺少 rename service、校验或事务结果；命令不得创建 Flask 数据库连接。

- [ ] **Step 3: 实现纯校验和原子条件更新**

`asset_display_name.py` 中：

```python
def normalize_name_body(value):
    if not isinstance(value, str):
        raise DisplayNameValidationError('显示名称主体必须是字符串')
    normalized = value.strip()
    if not 1 <= len(normalized) <= 100:
        raise DisplayNameValidationError('显示名称主体长度必须为 1 至 100 个字符')
    if normalized in {'.', '..'} or '/' in normalized or '\\' in normalized:
        raise DisplayNameValidationError('显示名称主体包含不允许的字符')
    if any(unicodedata.category(char) == 'Cc' for char in normalized):
        raise DisplayNameValidationError('显示名称主体包含控制字符')
    return normalized
```

先读取资产以获得来源扩展名和 before state，再执行 Core UPDATE：

```python
statement = (
    update(ImageAsset)
    .where(
        ImageAsset.id == asset_id,
        ImageAsset.status == 'active',
        ImageAsset.version == expected_version,
    )
    .values(
        display_name=full_name,
        version=ImageAsset.version + 1,
        updated_at=func.now(),
    )
    .returning(ImageAsset)
)
```

成功后 `db.session.add(AssetActivityRecord(...))` 并只提交一次。零行时 `expire_all()` 后重读，映射 404/archived/version conflict；冲突返回最新管理 DTO。异常统一 rollback。

蓝图只校验 JSON 形状、拒绝 bool 版本、取得/生成最多 64 字符 request ID，并映射领域结果。

- [ ] **Step 4: 编写两连接真实并发测试但不执行，并通过 Mock 分支测试**

使用两个独立 SQLAlchemy connection/session 和 `threading.Barrier` 同时提交 `expected_version=1`；断言一个成功、一个冲突、最终 version 2、成功活动记录恰好一条。若测试夹具的单连接绑定不适用，在同一随机 schema 上显式创建两条 engine connection，不得退化为 SQLite。

真实并发文件保留在 integration 套件并标记需要授权；实际只 Run 同 Step 2。

Expected: 全部 Mock rename/OCC 分支测试 PASS；真实并发状态为未执行。

- [ ] **Step 5: 检查未提交 diff**

Run `git diff --check && git status --short`；Expected: 无空白错误，无 commit。

---

### Task 3: 扩展普通搜索、向量结果与产品图片表示

**Files:**
- Modify: `backend/blueprints/image_assets.py`
- Modify: `backend/services/vector_search.py`
- Modify: `backend/blueprints/products_v2.py`
- Modify: `backend/models/image_asset.py`
- Modify: `backend/test/integration/test_image_asset_management.py`
- Modify: `backend/test/integration/test_vector_search.py`
- Modify: `backend/test/integration/test_write_paths.py`

**Interfaces:**
- Produces: 所有资产安全表示包含 `display_name`、`source_relative_path`、`version`。
- Preserves: 向量结果兼容字段 `relative_path`。

- [ ] **Step 1: 写失败的表示与双字段搜索测试**

管理 API 测试分别按自定义 `display_name` 和不可变 `source_relative_path` 命中，加入带 `%`、`_` 的名称证明按普通文本匹配；断言 active、assignment 和分页仍生效。

向量 shape 改为：

```python
assert set(result) == {
    'asset_id', 'model_number', 'display_name', 'source_relative_path',
    'relative_path', 'version', 'preview_url', 'similarity',
}
```

产品列表测试断言每个 `images[]` 同时返回三字段。

- [ ] **Step 2: 运行静态合同测试确认字段/搜索失败**

Run:

```bash
cd backend
python -m pytest test/test_issue_15_static_contract.py -k 'search or vector or product or representation' -v
```

Expected: FAIL，缺少双字段表达式、新字段或统一表示；命令不得连接数据库。

- [ ] **Step 3: 实现最小表示扩展与 LIKE 转义**

集中一个安全 DTO 函数供列表与 rename 复用。搜索模式：

```python
escaped = search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
pattern = f'%{escaped}%'
query = query.filter(or_(
    ImageAsset.display_name.ilike(pattern, escape='\\'),
    ImageAsset.source_relative_path.ilike(pattern, escape='\\'),
))
```

向量 SQL `SELECT` 增加三列，但不改 `WHERE status = 'active'`、ORDER BY、LIMIT 与 similarity。产品图片 DTO 同步三字段。

- [ ] **Step 4: 运行定向测试确认通过**

Run 同 Step 2。

Expected: 静态合同 PASS；真实向量顺序/相似度集成验证保留为未执行、需授权。

- [ ] **Step 5: 检查未提交 diff**

Run `git diff --check && git status --short`；Expected: 无 commit。

---

### Task 4: 以 TDD 实现结构化前端 rename API 与共享编辑器

**Files:**
- Modify: `frontend/src/types/product.ts`
- Modify: `frontend/src/services/productApi.ts`
- Modify: `frontend/src/services/productApi.test.ts`
- Create: `frontend/src/components/AssetDisplayNameEditor.tsx`
- Create: `frontend/src/components/AssetDisplayNameEditor.test.tsx`

**Interfaces:**
- Produces: `renameImageAsset(assetId, nameBody, expectedVersion)`。
- Produces: `ImageAssetRenameError.latest?: ImageAssetManagementItem`。
- Produces: `<AssetDisplayNameEditor asset onRenamed />`。

- [ ] **Step 1: 写失败的 API 与交互测试**

API 测试断言 POST body 和 409 latest：

```typescript
await renameImageAsset('asset-1', '新名称', 3);
expect(fetchMock).toHaveBeenCalledWith(
  expect.stringContaining('/api/image-assets/asset-1/rename'),
  expect.objectContaining({
    method: 'POST',
    body: JSON.stringify({ name_body: '新名称', expected_version: 3 }),
  })
);
```

编辑器测试覆盖：常驻编辑按钮、主名称/次路径、扩展只读、保存、Enter、取消、Esc、blur 不保存、一般失败保留草稿、409 更新服务器最新显示/版本但保留草稿并在第二次保存使用新版本。

- [ ] **Step 2: 运行 Vitest 确认模块缺失失败**

Run:

```bash
cd frontend
npm test -- --run src/services/productApi.test.ts src/components/AssetDisplayNameEditor.test.tsx
```

Expected: FAIL，缺少 rename API、类型或组件。

- [ ] **Step 3: 实现类型、结构化错误与编辑器状态机**

统一类型加入：

```typescript
display_name: string;
source_relative_path: string;
version: number;
```

错误类必须保留服务端错误码和 latest。编辑器内部维护 `draftBody`、`serverAsset`、`editing`、`saving`、`error`；冲突时只更新 `serverAsset`，不覆盖 draft。`onBlur` 不调用 save/cancel；`onKeyDown` 仅 Enter/Escape 触发显式动作。

- [ ] **Step 4: 运行定向 Vitest 确认通过**

Run 同 Step 2。

Expected: 两个测试文件全部 PASS。

- [ ] **Step 5: 检查未提交 diff**

Run `git diff --check && git status --short`；Expected: 无 commit。

---

### Task 5: 把已归款/未归款资产接入顶层工作台并更新搜索结果

**Files:**
- Modify: `frontend/src/components/UnassignedAssetGrid.tsx`
- Modify: `frontend/src/components/UnassignedAssetGrid.test.tsx`
- Modify: `frontend/src/components/ProductUpload.tsx`
- Modify: `frontend/src/components/ProductUpload.test.tsx`
- Modify: `frontend/src/components/ProductSearch.tsx`
- Modify: `frontend/src/services/productApi.ts`

**Interfaces:**
- Consumes: `AssetDisplayNameEditor`、`renameImageAsset` 与统一资产类型。
- Produces: `getImageAssets({ assignment, page, perPage, search })`。

- [ ] **Step 1: 写失败的顶层用户流程测试**

`ProductUpload.test.tsx` mock unassigned/assigned 响应，断言切换后调用：

```typescript
expect(api.getImageAssets).toHaveBeenLastCalledWith({
  assignment: 'assigned', page: 1, perPage: 24, search: '',
});
```

再从顶层页面点击卡片常驻编辑按钮，输入新主体并保存，断言 `renameImageAsset('asset-1', '新名称', 1)`；失败时输入仍在。Grid 测试断言主显示名称、次来源路径、已归款型号、搜索 placeholder/空状态均描述双字段。

- [ ] **Step 2: 运行顶层测试确认失败**

Run:

```bash
cd frontend
npm test -- --run \
  src/components/UnassignedAssetGrid.test.tsx \
  src/components/ProductUpload.test.tsx
```

Expected: FAIL，缺少 assignment 筛选、编辑器或新文案。

- [ ] **Step 3: 实现通用资产工作台编排**

`getImageAssets` 接受 `assignment: 'unassigned' | 'assigned' | 'all'`。ProductUpload 保存筛选状态，切换时清页码/选择并请求对应列表。卡片：

- 使用 `AssetDisplayNameEditor` 作为主信息；
- 来源路径作为次信息；
- assigned 显示型号；
- 只有未归款资产可勾选关联；
- rename 成功替换当前数组中的同 ID 项，不修改其他卡片。

ProductSearch 使用 `result.display_name` 为主标题、`result.source_relative_path` 为来源；复制仍复制来源路径。

- [ ] **Step 4: 运行全部定向前端测试与 build**

Run:

```bash
cd frontend
npm test -- --run \
  src/services/productApi.test.ts \
  src/components/AssetDisplayNameEditor.test.tsx \
  src/components/UnassignedAssetGrid.test.tsx \
  src/components/ProductUpload.test.tsx
npm run build
```

Expected: Vitest 全 PASS；`tsc && vite build` exit 0。

- [ ] **Step 5: 检查未提交 diff**

Run `git diff --check && git status --short`；Expected: 无 commit。

---

### Task 6: 汇总安全范围内的定向验证并准备独立风险审查

**Files:**
- Modify only if verification exposes Issue #15 defects.

**Interfaces:**
- Produces: 可复制的原始测试输出和风险审查输入。

- [ ] **Step 1: 运行后端纯单元/Mock/静态套件**

Run:

```bash
cd backend
python -m pytest test/test_asset_display_name_unit.py test/test_issue_15_static_contract.py -v
```

Expected: 全 PASS、0 skip、无数据库连接。所有 integration/PostgreSQL 测试明确不运行并列为待用户授权，不能宣称后端数据库语义已通过。

- [ ] **Step 2: 运行前端 Issue #15 定向套件和 build**

Run Task 5 Step 4 命令。

Expected: 全 PASS，build exit 0。

- [ ] **Step 3: 静态核对安全边界**

Run:

```bash
rg -n "apply_migration|migrate.*issue_15" backend/app.py backend/init_db.py
rg -n "OssObjectStorage|EmbeddingClient|DashScope" backend/services/asset_display_name.py backend/blueprints/image_assets.py
git diff --check
```

Expected: 前两项无匹配；`git diff --check` exit 0。不要运行任何真实迁移或 OSS 脚本。

- [ ] **Step 4: 委派独立 risk_reviewer**

审查范围固定为未提交 diff、Issue #15/父 PRD/ADR、定向测试原始输出。要求 reviewer 检查 schema/回滚、OCC race、审计原子性、字段遗漏、前端冲突草稿、外部写边界与基线差异；reviewer 不修改文件。

- [ ] **Step 5: 修正 reviewer 的有效发现并复跑相关测试**

每个修复先新增/收紧失败测试，再改最小实现；复跑 Task 6 Step 1/2。若 reviewer 无有效发现，不制造无关重构。

- [ ] **Step 6: 最终工作树核对**

Run:

```bash
git status --short --branch
git diff --stat
git diff --check
```

Expected: detached HEAD 基线仍为 088bb9f；仅 Issue #15 相关文件未提交；无 commit、push、部署或云端变更。

## Plan Self-Review

- **规格覆盖：** schema、回填、回滚兼容、活动记录、OCC、归档/重名/校验、双字段搜索、三类表示、已归款入口、前端交互与隔离测试均有对应 Task。
- **占位扫描：** 无 TBD/TODO/“适当处理”步骤；代码步骤给出明确签名、SQL/TS/Python 形状和命令。
- **类型一致性：** 后端统一返回 `display_name/source_relative_path/version`；向量 `relative_path` 仅兼容保留；前端 API 使用 `name_body/expected_version`，冲突统一读取 `latest`。
- **用户覆盖：** 计划中的 commit 模板已按明确禁令替换为 diff 检查；不触发实现 subagent，architect/risk reviewer 仅做明确授权的独立审查。
