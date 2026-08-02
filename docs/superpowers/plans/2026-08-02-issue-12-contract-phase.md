# Issue #12 Contract Phase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 停用 product_images 与本地 uploads 的活动路径，让私有 OSS 和 image_assets 成为唯一正式图片工作流，同时保留非破坏性的旧表兼容审计。

**Architecture:** 删除 Flask、Nginx、Docker、ORM 和初始化 SQL 中的旧路径，而不是 DROP 已有数据库表。新增显式、只读的旧表审计服务与 CLI；它只报告旧表是否存在及行数，非空时给出人工兼容迁移要求。产品上传、Kodo 迁移、图片搜索和预览继续通过现有 ImageAssetIngestService、image_assets 和签名 302 工作。

**Tech Stack:** Python 3.9+、Flask、Flask-SQLAlchemy、PostgreSQL 16/pgvector、pytest、Docker Compose、React/Vite。

## Global Constraints

- 不执行 DROP product_images、DELETE、云对象清理或对 backend/uploads 的物理删除。
- 不在 app.py、init_db.py、Docker 启动 SQL 或普通部署中加入隐式数据收缩。
- 图片正式存储只能是私有 OSS；Kodo 只读备份；预览只能经 /api/image-assets/<asset_id>/preview 的短时签名 302。
- 旧表非空只能输出独立兼容迁移清单，不允许自动转换、删除或覆盖。
- 已确认 TDD seams：部署/启动配置、显式兼容审计、私有预览与写路径、完整后端与前端回归。

---

## File Structure

| Path | Responsibility |
| --- | --- |
| backend/services/legacy_product_images.py | 只读检测旧表并返回稳定的兼容迁移审计结果。 |
| backend/scripts/audit_legacy_product_images.py | 显式运行审计并输出脱敏 JSON；不是应用启动钩子。 |
| backend/test/test_legacy_product_images_audit.py | 无副作用连接替身验证表不存在、空表和非空表三个审计结果。 |
| backend/app.py、frontend/nginx.conf、docker-compose.yml | 删除本地 uploads 的活动服务与持久化挂载。 |
| backend/models/product.py、backend/models/__init__.py、backend/init_db.py、postgres/init/01_init.sql | 移除新库创建旧表/旧索引的职责，保留 image_assets schema。 |
| backend/services/ingest.py、backend/blueprints/oss.py、backend/scripts/batch_upload_oss.py | 删除旧本地落盘、公开 URL 拼接和未注册旧蓝图。 |
| AGENTS.md、backend/.env.example、backend/scripts/README_OSS_MIGRATION.md | 收束为 OSS 正式源、Kodo 只读备份和显式兼容审计的唯一运维说明。 |

### Task 1: Add an explicit, read-only legacy-table audit

**Files:**

- Create: backend/services/legacy_product_images.py
- Create: backend/scripts/audit_legacy_product_images.py
- Test: backend/test/test_legacy_product_images_audit.py

**Interfaces:**

- Consumes: sqlalchemy.Connection and static SQL only; it must not import ProductImage.
- Produces: LegacyProductImagesAudit with table_exists: bool, row_count: int | None, compatibility_required: bool, required_actions: tuple[str, ...].
- Produces: audit_legacy_product_images(connection) -> LegacyProductImagesAudit and python -m scripts.audit_legacy_product_images.

- [ ] **Step 1: Write the failing no-side-effect audit tests**

~~~python
from services.legacy_product_images import audit_legacy_product_images


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _AuditConnection:
    def __init__(self, table_exists, row_count=None):
        self.table_exists = table_exists
        self.row_count = row_count
        self.statements = []

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if 'to_regclass' in sql:
            return _ScalarResult(self.table_exists)
        return _ScalarResult(self.row_count)


def test_audit_reports_absent_legacy_table():
    connection = _AuditConnection(table_exists=False)
    audit = audit_legacy_product_images(connection)

    assert audit.table_exists is False
    assert audit.row_count is None
    assert audit.compatibility_required is False
    assert audit.required_actions == ()
    assert len(connection.statements) == 1


def test_audit_reports_empty_legacy_table():
    connection = _AuditConnection(table_exists=True, row_count=0)
    audit = audit_legacy_product_images(connection)

    assert audit.table_exists is True
    assert audit.row_count == 0
    assert audit.compatibility_required is False
    assert len(connection.statements) == 2


def test_audit_requires_manual_migration_for_nonempty_legacy_table():
    connection = _AuditConnection(table_exists=True, row_count=1)
    audit = audit_legacy_product_images(connection)

    assert audit.table_exists is True
    assert audit.row_count == 1
    assert audit.compatibility_required is True
    assert audit.required_actions == (
        '制定独立兼容迁移清单并取得明确授权',
        '在迁移完成前不得 DROP、DELETE 或转换 product_images',
    )
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: cd backend && python -m pytest test/test_legacy_product_images_audit.py -v

Expected: collection fails because services.legacy_product_images does not exist.

- [ ] **Step 3: Write minimal implementation**

~~~python
from __future__ import annotations

@dataclass(frozen=True)
class LegacyProductImagesAudit:
    table_exists: bool
    row_count: int | None

    @property
    def compatibility_required(self) -> bool:
        return self.row_count not in (None, 0)

    @property
    def required_actions(self) -> tuple[str, ...]:
        if not self.compatibility_required:
            return ()
        return (
            '制定独立兼容迁移清单并取得明确授权',
            '在迁移完成前不得 DROP、DELETE 或转换 product_images',
        )

    def to_dict(self) -> dict[str, object]:
        return {
            'table': 'product_images',
            'table_exists': self.table_exists,
            'row_count': self.row_count,
            'compatibility_required': self.compatibility_required,
            'required_actions': list(self.required_actions),
        }


def audit_legacy_product_images(connection) -> LegacyProductImagesAudit:
    exists = connection.execute(
        text("SELECT to_regclass('public.product_images') IS NOT NULL")
    ).scalar_one()
    if not exists:
        return LegacyProductImagesAudit(False, None)
    row_count = connection.execute(
        text('SELECT COUNT(*) FROM product_images')
    ).scalar_one()
    return LegacyProductImagesAudit(True, int(row_count))
~~~

The CLI must call create_app(), enter its app context, call this function with db.session.connection(), then print json.dumps(audit.to_dict(), ensure_ascii=False). It must not register a Flask route or be called from create_app()/init_database().

- [ ] **Step 4: Run test to verify it passes**

Run: cd backend && python -m pytest test/test_legacy_product_images_audit.py -v

Expected: all three audit cases pass without executing DDL/DML against any database.

- [ ] **Step 5: Commit**

~~~bash
git add backend/services/legacy_product_images.py backend/scripts/audit_legacy_product_images.py backend/test/test_legacy_product_images_audit.py
git commit -m "feat(contract): add read-only legacy image audit"
~~~

### Task 2: Contract the active runtime and new-database schema

**Files:**

- Modify: backend/app.py
- Modify: backend/models/product.py
- Modify: backend/models/__init__.py
- Modify: backend/blueprints/products_v2.py
- Modify: backend/init_db.py
- Modify: postgres/init/01_init.sql
- Modify: docker-compose.yml
- Modify: frontend/nginx.conf
- Modify: backend/test/integration/conftest.py
- Create: backend/test/test_contract_configuration.py

**Interfaces:**

- Consumes: ImageAsset model and ImageAssetIngestService unchanged.
- Produces: no ProductImage import, no /uploads/<path:filename> endpoint, no backend/uploads:/app/uploads bind mount, and no product_images DDL in new-database initialization.
- Preserves: /api/image-assets/<uuid:asset_id>/preview returns its existing signed 302 response.

- [ ] **Step 1: Write failing configuration-contract tests**

~~~python
def test_app_does_not_expose_legacy_uploads_route(client):
    response = client.get('/uploads/product_images/legacy.png')
    assert response.status_code == 404


def test_new_database_schema_does_not_create_product_images(app):
    table_names = set(inspect(db.engine).get_table_names())
    assert 'image_assets' in table_names
    assert 'product_images' not in table_names


def test_deployment_files_do_not_persist_or_proxy_legacy_uploads():
    compose = repo_file('docker-compose.yml').read_text(encoding='utf-8')
    nginx = repo_file('frontend/nginx.conf').read_text(encoding='utf-8')
    init_sql = repo_file('postgres/init/01_init.sql').read_text(encoding='utf-8')
    assert './backend/uploads:/app/uploads' not in compose
    assert 'location /uploads/' not in nginx
    assert 'CREATE TABLE IF NOT EXISTS product_images' not in init_sql
~~~

Define repo_file from Path(__file__).resolve().parents[2], never from the process working directory. Adapt the PostgreSQL integration fixture so it creates only active SQLAlchemy models and does not build a legacy HNSW index.

- [ ] **Step 2: Run test to verify it fails**

Run: cd backend && python -m pytest test/test_contract_configuration.py -v

Expected: route and static configuration assertions fail while the legacy path is active.

- [ ] **Step 3: Write minimal implementation**

~~~python
# backend/models/product.py: retain Product only; delete ProductImage.
# backend/models/__init__.py:
from .product import Product
from .image_asset import ImageAsset
__all__ = ['db', 'Product', 'ImageAsset']

# backend/blueprints/products_v2.py: remove ProductImage imports and the
# product/batch delete guards that query legacy rows. Existing Product delete
# behavior continues to detach ImageAsset by its ON DELETE SET NULL FK.

# backend/app.py: delete UPLOAD_FOLDER, ALLOWED_EXTENSIONS,
# product_images mkdir, and serve_upload. Retain MAX_CONTENT_LENGTH,
# products_v2_bp, image_assets_bp, dataset route, and health route.

# backend/init_db.py: retain CREATE EXTENSION, db.create_all(), and only
# idx_image_assets_vector_active_hnsw. Delete all product_images CREATE,
# ALTER, index and COUNT statements.
~~~

Delete product_images table, indexes, comments and ANALYZE from postgres/init/01_init.sql. Remove the uploads bind mount from Compose and the /uploads/ proxy block from Nginx. Do not delete backend/uploads from disk and do not add cleanup commands.

- [ ] **Step 4: Run test to verify it passes**

Run: cd backend && python -m pytest test/test_contract_configuration.py test/test_image_asset_preview.py test/integration/test_image_asset_search.py -v

Expected: configuration contract and signed-preview/image-search behavior pass.

- [ ] **Step 5: Commit**

~~~bash
git add backend/app.py backend/models/product.py backend/models/__init__.py backend/blueprints/products_v2.py backend/init_db.py postgres/init/01_init.sql docker-compose.yml frontend/nginx.conf backend/test/integration/conftest.py backend/test/test_contract_configuration.py
git commit -m "refactor(contract): remove active legacy image paths"
~~~

### Task 3: Remove retired local-write and public-URL code

**Files:**

- Delete: backend/services/ingest.py
- Delete: backend/blueprints/oss.py
- Delete: backend/scripts/batch_upload_oss.py
- Delete: backend/test/integration/test_dedup.py
- Delete: backend/test/integration/test_ingest_cli.py
- Modify: backend/test/integration/test_schema.py
- Modify: backend/test/integration/test_write_paths.py
- Modify: backend/test/test_kodo_preflight.py

**Interfaces:**

- Consumes: public Product CRUD and image asset preview/search APIs.
- Produces: no importable local ImageIngestService/ProductImage path, no unregistered public OSS blueprint, and no executable public-URL batch uploader.
- Preserves: scripts.ingest_images write mode refuses before scanning; scripts.migrate_oss_path refuses before writing; scripts.migrate_kodo_to_oss remains the sole migration entry.

- [ ] **Step 1: Write a failing retired-code reference audit test**

~~~python
def test_retired_code_is_not_importable_or_referenced():
    assert importlib.util.find_spec('services.ingest') is None
    assert importlib.util.find_spec('blueprints.oss') is None
    assert not repo_file('backend/scripts/batch_upload_oss.py').exists()
    active_sources = [
        repo_file('backend/app.py'),
        repo_file('backend/blueprints/products_v2.py'),
        repo_file('backend/models/__init__.py'),
    ]
    assert all('ProductImage' not in source.read_text(encoding='utf-8')
               for source in active_sources)
~~~

Add this to test_contract_configuration.py. Task 2 has already removed ProductImage so Task 3 verifies the remaining retired module files; this is a deletion seam rather than a second ORM-removal test. In test_write_paths.py, replace ProductImage queries and UPLOAD_FOLDER-derived assertions with existing public Product API assertions. Remove its two tests that insert legacy rows; Task 1 covers nonempty compatibility without keeping an ORM write path alive.

- [ ] **Step 2: Run test to verify it fails**

Run: cd backend && python -m pytest test/integration/test_write_paths.py -v

Expected: failure because the retired services.ingest, blueprints.oss and batch_upload_oss paths still exist before cleanup.

- [ ] **Step 3: Write minimal implementation**

Delete the three listed legacy source files and the two listed legacy test modules. In test_schema.py, remove ProductImage fixtures/tests and replace the coexistence assertion with:

~~~python
table_names = set(inspect(db.engine).get_table_names())
assert 'image_assets' in table_names
assert 'product_images' not in table_names
~~~

In test_kodo_preflight.py, retain the refusal test for scripts.migrate_oss_path but remove expectations involving writable product_images.oss_path. Keep scripts/ingest_images.py as read-only inventory and preserve its refusal before scan/embedding; cover that behavior in test_contract_configuration.py without importing ProductImage.

- [ ] **Step 4: Run test to verify it passes**

Run: cd backend && python -m pytest test/integration/test_write_paths.py test/integration/test_schema.py test/test_kodo_preflight.py -v

Expected: Product CRUD persists and reads ImageAsset, archived/unassigned assets retain their behavior, and no test imports deleted legacy code.

- [ ] **Step 5: Commit**

~~~bash
git add -u backend/services/ingest.py backend/blueprints/oss.py backend/scripts/batch_upload_oss.py backend/test/integration/test_dedup.py backend/test/integration/test_ingest_cli.py backend/test/integration/test_schema.py backend/test/integration/test_write_paths.py backend/test/test_kodo_preflight.py
git commit -m "refactor(contract): retire legacy image write code"
~~~

### Task 4: Consolidate operational documentation and static audit

**Files:**

- Modify: AGENTS.md
- Modify: backend/.env.example
- Modify: backend/scripts/README_OSS_MIGRATION.md
- Modify: docs/product-image-search-workflow.svg
- Test: backend/test/test_contract_configuration.py

**Interfaces:**

- Consumes: scripts.migrate_kodo_to_oss and scripts.audit_legacy_product_images as documented migration/audit commands.
- Produces: one operational narrative: Kodo read-only backup → private OSS → image_assets → signed preview/search; compatibility audit is separate from migration.

- [ ] **Step 1: Write failing documentation assertions**

~~~python
def test_operational_docs_name_oss_as_authoritative_store():
    agents = repo_file('AGENTS.md').read_text(encoding='utf-8')
    migration = repo_file(
        'backend/scripts/README_OSS_MIGRATION.md'
    ).read_text(encoding='utf-8')
    assert 'OSS 已成为正式图片源' in agents
    assert 'Kodo 只读备份' in agents
    assert 'python -m scripts.audit_legacy_product_images' in migration
    assert 'python -m scripts.ingest_images --root' not in agents
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: cd backend && python -m pytest test/test_contract_configuration.py::test_operational_docs_name_oss_as_authoritative_store -v

Expected: failure because AGENTS.md still lists the local legacy ingestion flow as standard.

- [ ] **Step 3: Write minimal documentation changes**

Rewrite AGENTS.md image architecture, schema, vector-search, Docker volume, backup and operational sections to name only image_assets. Mention product_images only as an unmodified retired table requiring python -m scripts.audit_legacy_product_images before a separately authorized migration. Retain QINIU_* in backend/.env.example only if migrate_kodo_to_oss reads them; label them Kodo 只读迁移来源 and never a public URL source. Update the SVG label from product_images to image_assets and make the result path the private preview endpoint. Update README_OSS_MIGRATION.md to lead with Kodo → private OSS and document audit output interpretation.

- [ ] **Step 4: Run tests and the reference audit**

Run: cd backend && python -m pytest test/test_contract_configuration.py -v

Run: rg -n "ProductImage|ImageIngestService|CREATE TABLE IF NOT EXISTS product_images|idx_product_images|/uploads/|build_public_url" backend docker-compose.yml frontend/nginx.conf postgres/init AGENTS.md --glob '!backend/reports/**'

Expected: tests pass. The audit has no active-code matches; accepted matches are only retired-script refusal text and explicitly marked compatibility documentation.

- [ ] **Step 5: Commit**

~~~bash
git add AGENTS.md backend/.env.example backend/scripts/README_OSS_MIGRATION.md docs/product-image-search-workflow.svg backend/test/test_contract_configuration.py
git commit -m "docs(contract): document oss-only image workflow"
~~~

### Task 5: Perform final verification and record contract evidence

**Files:**

- Modify if evidence changes the approved design: docs/superpowers/specs/2026-08-02-issue-12-contract-design.md

**Interfaces:**

- Consumes: every active image endpoint plus the explicit audit CLI.
- Produces: fresh evidence that no normal app path creates/deletes legacy data and private OSS image search remains functional.

- [ ] **Step 1: Run backend unit and integration suites**

Run: cd backend && python -m pytest test/ --ignore=test/integration -v

Run: cd backend && python -m pytest test/integration/ -v

Expected: all collected tests pass; database-unavailable integration tests may report skipped according to the existing fixture, not failed.

- [ ] **Step 2: Validate initialization and explicit audit against PostgreSQL**

Run: cd backend && python init_db.py

Run: cd backend && python -m scripts.audit_legacy_product_images

Run: docker compose exec -T db psql -U "$DB_USER" -d "$DB_NAME" -c "SELECT to_regclass('public.product_images') AS legacy_table, COUNT(*) AS image_assets FROM image_assets;"

Expected: init_db.py creates only image_assets indexes; audit is read-only; final SQL reports retained-table state and asset count without modifying either.

- [ ] **Step 3: Verify deployment configuration and frontend production build**

Run: docker compose config -q

Run: cd frontend && npm run build

Expected: Compose configuration is valid and Vite exits 0. Existing Browserslist or chunk-size warnings may be reported but must not be errors.

- [ ] **Step 4: Run the real search regression manually**

With the deployed service and an approved query image, POST the existing image-search endpoint, confirm the first result has an asset_id/source relative path, and GET its /api/image-assets/<asset_id>/preview URL. Record the HTTP 302 result and confirm no returned URL uses /uploads/ or a permanent public OSS/Kodo prefix.

- [ ] **Step 5: Re-read the design and commit only verified documentation adjustments**

Run: git diff --check

Run: git status --short

If a verified behavior differs, update the design document to observed behavior, rerun git diff --check, then commit:

~~~bash
git add docs/superpowers/specs/2026-08-02-issue-12-contract-design.md
git commit -m "docs(contract): record verified issue 12 evidence"
~~~

If no behavior differs, do not create an empty commit.
