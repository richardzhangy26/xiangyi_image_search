# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Qoder, etc.) when working with code in this repository.

## 项目概览

**电子产品配件图像搜索系统 (Electronic Accessories Image Search System)** 面向跨境电商卖家和批发采购，使用通义多模态 embedding 与 PostgreSQL/pgvector 完成以图搜款。

- 用户上传一张图片后，系统从 image_assets 检索视觉相似的图片资产。
- **OSS 已成为正式图片源**：原图和规范化预览都存放在私有 Aliyun OSS 的隔离前缀中。
- Kodo 是迁移期间的**Kodo 只读备份**，只能由受控迁移命令读取，不能作为公开 URL 来源。
- PostgreSQL 保存商品元数据、图片资产元数据和向量；凭证只保留在本地环境文件中。

## 架构

### 后端 (Flask + Python)

- Flask + Flask-SQLAlchemy。
- PostgreSQL 16 + pgvector（数据库 image_search），由 Docker 提供。
- DashScope tongyi-embedding-vision-plus-2026-03-06 生成 1024 维向量；向量在数据库内按余弦距离检索。
- 无进程内向量索引，服务重启不会丢失检索数据。

### 正式图片工作流

1. scripts.migrate_kodo_to_oss 对 Kodo 做只读 preflight、盘点和经授权的迁移；Kodo 不执行 Put/Delete。
2. 迁移或兼容产品上传经过 ImageAssetIngestService：在私有 OSS 中无覆盖写入原图和 preview-v1 预览，然后写入 image_assets 及其向量。
3. 产品管理页的独立图片导入在完成图片校验与私有 OSS 无覆盖写入后创建 image_import_items 排队项并立即返回。
4. 独立 worker 用 PostgreSQL `FOR UPDATE SKIP LOCKED` 与租约领取任务，在请求外生成 embedding；只有模型和 1024 维有限向量校验通过后，才在同一事务中创建正式 image_assets 并完成任务。
5. VectorSearchService 只检索 image_assets.status = active 的向量。
6. API 只返回资产 ID 和 /api/image-assets/<asset_id>/preview；该入口生成短时签名 URL 并返回私有 302，不保存或拼接公开 URL。签名过期时刻按 OSS_SIGNED_URL_TTL_SECONDS 长度的时间窗口对齐，同一资产在窗口内 URL 稳定，302 与 OSS 响应均携带私有 Cache-Control，浏览器在窗口内刷新不重复消耗 OSS 出口流量。
7. 未归款资产可原地移入回收站并批量恢复；恢复只改变生命周期字段和版本，复用原资产 ID、向量及 OSS 绑定，不重新上传或生成 embedding。
8. 永久清除 HTTP 控制面已存在但默认关闭：未配置 `PURGE_ADMIN_TOKEN` 或证据目录为空时不可用；`pipeline_available()` 恒为 False。能写 `PURGE_GATE_EVIDENCE_DIR` 即能让安全门报就绪，属主机信任边界。真实启用仍待现场证据与后续票授权。

product_images 是**未修改的退休兼容表**，不属于新库 schema、应用 ORM 或活动写路径。任何另行授权的兼容迁移前，先运行 python -m scripts.audit_legacy_product_images，根据只读审计结果制作人工迁移清单；本仓库不自动迁移、删除或覆盖它，也不物理清理旧对象。

### 关键文件

- backend/app.py - Flask 初始化、数据库配置、CORS、蓝图注册和日志。
- backend/models/image_asset.py - 独立 ImageAsset 模型；型号可空，商品删除时设为 NULL。
- backend/models/image_import_item.py - queued、embedding、completed、failed、awaiting_retry、cancelled、abandoned 七态持久导入项及 worker claim 租约。
- backend/services/image_normalizer.py - EXIF、尺寸、透明背景、动图首帧和 2.5 MiB 上限。
- backend/services/object_storage.py - 私有 OSS HEAD、无覆盖上传、worker 私有下载和短时签名下载。
- backend/services/asset_ingest.py - 来源图片到 OSS、embedding 和 PostgreSQL 的纵向入库服务。
- backend/services/image_import_worker.py - 多实例任务领取、结果校验、原子资产提升和失败落库。
- backend/services/asset_recycle_bin.py - 归档资产只读列表、计数与最多 100 张的原子批量恢复服务。
- backend/services/admin_auth.py - 永久清除控制面共享令牌认证；未配置则失败关闭。
- backend/services/purge_safety_gate.py - 五项安全门合取判定与证据探针；`pipeline_available()` 默认 False。
- backend/blueprints/admin_purge.py - `/api/admin/purge` 准备状态与拒绝写路径。
- backend/services/vector_search.py - pgvector 检索服务。
- backend/services/kodo_source.py - Kodo S3 只读对象来源。
- backend/services/purge_object_backup.py - 永久清除前的完整引用规划、不可覆盖对象备份与 final manifest。
- backend/services/purge_object_restore.py - 对象副本复验和仅隔离 Bucket 恢复。
- backend/services/purge_object_storage.py - 正式源 Head/Get-only 与隔离目标 write-once 的角色专用 OSS Adapter。
- backend/scripts/migrate_kodo_to_oss.py - 受控 Kodo → 私有 OSS 迁移入口。
- backend/scripts/manage_purge_object_backups.py - 只提供既有对象副本复验与隔离恢复；不提供创建或删除命令。
- backend/scripts/audit_legacy_product_images.py - 独立、只读的退休兼容表审计入口。
- backend/scripts/run_image_import_worker.py - 独立持久导入 worker 进程入口。
- backend/blueprints/products_v2.py - Product CRUD、CSV 导入和产品图片资产入库（/api/products）。
- backend/blueprints/image_assets.py - 活跃/归档图片资产列表、归款、本地导入（POST /api/image-assets/import，source_provider=local-import，只写未归款资产）、归档、恢复和私有预览 302（/api/image-assets）。
- backend/blueprints/image_imports.py - 图片导入排队、持久任务列表和详情、手工重试、单个与批量取消、放弃项恢复（/api/image-imports）。
- backend/models/product.py - Product 商品元数据模型。
- backend/init_db.py - 创建 pgvector 扩展、模型表和 image_assets HNSW 索引。
- postgres/init/01_init.sql - Docker 首次启动 SQL；应与模型保持一致。

### Embedding 模型

| 项目 | 值 |
| --- | --- |
| Model | tongyi-embedding-vision-plus-2026-03-06（Qwen3 base） |
| Dimension | 1024（数据库列为 vector(1024)） |
| Metric | Cosine distance |
| Text support | 30+ 语言；未来可用同一向量空间做文本搜图 |

不要把不同模型的向量混入同一张资产表。更换模型时必须重新生成全部资产向量。

## 向量搜索实现

**核心类**：VectorSearchService（backend/services/vector_search.py）。

~~~
search_similar_images(image_path, top_k=10, request_id=None) -> list
search_by_vector(vector, top_k=10, request_id=None) -> list
~~~

查询只读活跃资产，并在事务内设置 HNSW 搜索参数；每个结果包含 asset_id、可选 model_number、来源相对路径、相似度和私有预览入口。相似度按 min(1.0, max(0.0, 1.0 - distance)) 返回。

~~~
SET LOCAL hnsw.ef_search = :ef;
SELECT id, model_number, source_relative_path,
       vector <=> CAST(:query_vector AS vector) AS distance
FROM image_assets
WHERE status = 'active'
ORDER BY vector <=> CAST(:query_vector AS vector)
LIMIT :top_k;
~~~

SET LOCAL 保证连接归还连接池时不污染后续请求；服务在成功和异常路径都会 rollback。

## 前端

- React 18 + TypeScript + Vite；Ant Design 5 和 Tailwind CSS。
- ProductSearch（以图搜款）和 ProductUpload（产品管理）是当前路由组件。
- ProductUpload 默认展示待归款图片，支持按来源路径搜索、分页、多选并关联既有型号；资产工作台提供独立的“添加产品”按钮（仅型号快速创建，选中图片时一并关联），并提供独立回收站标签、图片导入入口、未解决任务徽标和持久任务抽屉（抽屉内支持手工重试与取消）；刷新后任务状态从服务端恢复。创建产品仅型号必填（其余字段空占位、可后续补全），页面不自动推断型号，也不提供解绑、跨型号改绑或永久清除。回收站有可折叠管理员面板，只读展示永久清除准备状态，任何状态下都没有执行按钮。
- 前端通过 /api/ 访问后端；Nginx 只代理 API，不提供本地图片静态源。
- 图片卡片使用私有预览入口，浏览器跟随 302 获取短时签名地址；签名 URL 按时间窗口对齐且响应带私有缓存头，窗口内刷新直接命中浏览器缓存。

## Docker 部署

docker-compose.yml 包含 db、backend、worker、frontend 四个服务，网络为 app-network：

| 服务 | 容器 | 端口 | 说明 |
| --- | --- | --- | --- |
| db | fashion-crm-db | 127.0.0.1:5433 → 5432 | pgvector PostgreSQL 16 |
| backend | fashion-crm-backend | 0.0.0.0:5000 → 5000 | Gunicorn；健康检查 /api/health |
| worker | fashion-crm-image-import-worker | 无 | PostgreSQL 持久队列的独立 embedding worker |
| frontend | fashion-crm-frontend | 0.0.0.0:80 → 80 | Nginx 静态构建和 API 代理 |

- 数据库卷 postgres_data 保存 PostgreSQL 数据。
- 后端只挂载 ./backend/data:/app/data 供运行时数据使用；正式图片不落盘到容器文件系统，原图和预览均在私有 OSS。
- postgres/init/*.sql 只在空数据库首次启动时执行，创建扩展、商品表、image_assets 和 HNSW 索引。
- docker compose down -v 会删除数据库卷和向量，执行前必须确认已有备份；不执行旧图片对象的物理清理。

~~~
docker compose up -d
docker compose build backend
docker compose build frontend && docker compose up -d frontend
docker compose logs -f backend
docker compose down
~~~

## 备份与恢复

~~~
docker exec fashion-crm-db pg_dump -U postgres image_search > backup_$(date +%Y%m%d).sql
~~~

数据库备份覆盖商品、image_assets 元数据和向量。OSS 原图、预览及 Kodo 只读备份由对象存储侧的私有备份/版本策略负责；不要在应用脚本中删除、覆盖或公开它们。恢复时先恢复 PostgreSQL，再核对 OSS 对象和 image_assets 的来源绑定。

永久清除对象备份使用同一 `purge_batch_id` 绑定 PostgreSQL 恢复点，先写不可变 `plan.json`，逐项 HEAD/下载校验后写 payload，最后才写 `manifest.json`。当前没有 PostgreSQL 引用快照生产 Adapter，也没有对象备份创建 CLI；真实永久清除 gate 保持关闭。既有清单只能显式复验或恢复到独立隔离 Bucket：

~~~
python scripts/manage_purge_object_backups.py verify-copies --manifest <object-manifest>
python scripts/manage_purge_object_backups.py restore-isolated --manifest <object-manifest> --restore-run-id <id> --acknowledge-isolated
~~~

## 本地开发

~~~
cd backend
cp .env.example .env       # 填写凭证和数据库连接
python init_db.py
python app.py              # http://0.0.0.0:5000

cd ../frontend
npm install
npm run dev                # http://localhost:5173
~~~

数据库连接优先读取 DATABASE_URL，否则组合 DB_HOST、DB_PORT、DB_NAME、DB_USER、DB_PASSWORD。

## 环境变量

backend/.env（由 .env.example 复制）中的必填项：

- DASHSCOPE_API_KEY - DashScope embedding 凭证。
- DB_HOST、DB_PORT、DB_NAME、DB_USER、DB_PASSWORD - PostgreSQL 连接。
- OSS_ACCESS_KEY_ID、OSS_ACCESS_KEY_SECRET、OSS_ENDPOINT、OSS_BUCKET_NAME - 私有 OSS 原图/预览和签名下载。
- QINIU_ACCESS_KEY、QINIU_SECRET_KEY、QINIU_BUCKET_NAME、QINIU_REGION - **Kodo 只读迁移来源**，仅供 migrate_kodo_to_oss 的 preflight/迁移读取，绝不生成公开 URL。

可选项：DATABASE_URL、QINIU_S3_BUCKET_NAME、OSS_IMAGE_BASE_PREFIX、OSS_SIGNED_URL_TTL_SECONDS、IMAGE_PREVIEW_MAX_EDGE、IMAGE_PREVIEW_MAX_MB、IMAGE_MAX_PIXELS、IMAGE_NORMALIZATION_VERSION、SEARCH_OVERSAMPLE、LOG_LEVEL、PURGE_ADMIN_TOKEN、PURGE_ADMIN_ACTOR_ID、PURGE_GATE_EVIDENCE_DIR。未设置管理员令牌或证据目录时永久清除控制面保持关闭。Docker Compose 会把容器内的数据库地址覆盖为 db:5432。

独立 ops 环境 `backend/.env.backup` 还使用 `BACKUP_OSS_*`、`PURGE_SOURCE_OSS_*` 与 `PURGE_RESTORE_OSS_*`。它们不得注入 Flask/Gunicorn 或 Docker 日常应用服务：正式源角色仅 Head/Get，备份角色仅 Put-if-absent/Head/Get，隔离角色仅隔离前缀 Put-if-absent/Head/Get，三类 ops 凭证均不能与应用 `OSS_*` 复用。`PURGE_RESTORE_ISOLATED` 默认必须为 `0`。

## 数据库结构

**数据库**：image_search · **扩展**：vector

### products

主键为 model_number VARCHAR(100)；保存摄影师文件、阿里商品 URL、分类、规格、价格、参考链接以及创建/更新时间。CSV 导入只创建商品元数据。

### image_assets

| 字段 | 说明 |
| --- | --- |
| id | UUID 主键 |
| model_number | 可空外键；未归款资产为 NULL |
| source_provider / source_bucket / source_relative_path / source_revision | 来源身份和修订号 |
| display_name / version | 可编辑显示名称与乐观并发版本 |
| oss_path / preview_oss_path | 私有 OSS 原图和规范化预览对象键 |
| content_hash / source_size / source_mime_type / source_width / source_height | 原图内容和尺寸元数据 |
| vector / embedding_model / embedding_dimension | 1024 维 pgvector 及模型绑定 |
| sort_order | 商品内图片展示顺序；0 即主图，未归款资产无意义（默认 0） |
| normalization_version / status / archived_at | 规范化版本；active 或 archived 生命周期及归档时间 |

索引包括内容哈希、型号、状态和只针对 active 资产的 HNSW cosine 索引。退休兼容表保持原样，不由新库初始化或应用写路径创建；如需兼容迁移，先单独执行只读审计并取得授权。

产品图片接口按 (sort_order, created_at, id) 升序返回，is_primary 由排序后首位派生；新上传与归款图片追加队尾，编辑弹窗拖拽排序随 PUT /api/products/<model> 的 image_order 单事务保存。存量数据库通过 python -m scripts.migrate_image_asset_sort_order（幂等）补列并回填，不在应用启动时隐式执行。

### image_import_items

持久保存已通过图片校验并写入私有 OSS 的导入项，状态为 queued、embedding、completed、failed、awaiting_retry、cancelled、abandoned。表中保存四列来源身份、对象绑定、规范化元数据、预期模型/维度、正式 asset_id、重试预算与 next_retry_at、取消/放弃时间以及 claim token/generation/owner/lease；worker 崩溃后只通过过期租约恢复领取。瞬时失败按错误分类指数退避自动重试（默认最多 5 次），失败、取消与放弃项都不会产生正式资产或占位向量。

## 产品与迁移操作

POST /api/products/import-csv 接受 UTF-8、GBK、GB2312 或 UTF-8-SIG CSV，必填列为 model_number、photographer_file、alibaba_product_url、category。产品图片通过产品 API 写入私有 OSS 和 image_assets，不会产生公开对象地址。

Kodo 迁移在 backend 目录运行：

~~~
python -m scripts.migrate_kodo_to_oss --preflight
python -m scripts.migrate_kodo_to_oss --dry-run --report-path reports/kodo-dry-run.json
python -m scripts.migrate_kodo_to_oss --verify-selection --selection-manifest reports/issue-10/selection.json --report-path reports/issue-10/selection-verification.json
python -m scripts.migrate_kodo_to_oss --pilot 10 --selection-manifest reports/issue-10/selection.json --verified-selection-report reports/issue-10/selection-verification.json --report-path reports/kodo-pilot.json
~~~

不提供 --pilot 或 --full 时命令只读；全量写入还需要受控授权、数据库恢复点和新鲜 preflight/dry-run 报告。迁移报告包含对象统计、阶段状态、冲突和脱敏失败原因，不保存凭证或签名 URL。兼容审计是另一条命令，不能用迁移命令替代：

~~~
python -m scripts.audit_legacy_product_images
~~~

审计只检查退休表是否存在及行数：表不存在或为空表示没有待处理的旧行；非空表示必须由人工制定、单独批准的兼容迁移清单。审计不会扫描文件、写入数据库、上传 OSS、删除表或删除对象。

## 测试

~~~
cd backend
python -m pytest test/ \
  --ignore=test/test.py \
  --ignore=test/test_pgvector.py \
  --ignore=test/benchmark_search.py -v
python -m pytest test/integration/ -v
python -m pytest test/ \
  --ignore=test/integration \
  --ignore=test/test.py \
  --ignore=test/test_pgvector.py \
  --ignore=test/benchmark_search.py -v
~~~

集成测试使用 DB_HOST:DB_PORT（默认 localhost:5433）上的独立 image_search_test 数据库；连接不可用时会自动 skip。

- test/test.py 是真实 OSS 连通性脚本，会列举对象并上传、下载、删除 test_oss_connection.txt；代理不得自动运行，只有用户明确授权且确认隔离 bucket 后才能执行。
- test/test_pgvector.py 和 test/benchmark_search.py 是手工基准脚本，不是 pytest 用例；代理不得把它们纳入默认测试或验证命令。
- 子代理只运行主线程明确批准的定向测试命令，不得自行把定向测试扩成全量测试、真实云写入或数据库基准操作。

## 重要约束

- 不执行 DROP、DELETE、覆盖上传或云对象清理来“迁移”旧数据；所有收缩动作必须另行授权并可回滚。
- 图片正式来源始终是私有 OSS，Kodo 只读备份；预览始终经过 /api/image-assets/<asset_id>/preview 的短时签名 302，不在代码中拼接公开对象地址。
- 回收站只保存 image_assets 的归档状态；恢复不得复制对象、重算向量或更改来源身份，自动过期与永久清除不属于当前应用能力。
- 图片导入以 source_provider、source_bucket、source_relative_path、source_revision 组成的来源身份去重：相同来源身份和同一内容返回既有结果；同一来源身份但内容不同返回来源冲突且绝不覆盖；命中归档来源身份时返回回收站结果且不自动恢复；不同来源路径即使内容相同也分别创建资产，只允许复用兼容预览和向量。
- 持久异步图片导入任务只由独立 worker 处理；HTTP 请求内不启动线程或可靠内存队列。瞬时失败按错误分类指数退避自动重试并受尝试预算约束，手工重试、取消与放弃项恢复只改持久状态；当前没有暂存对象清理或永久删除能力。现存 POST /api/image-assets/import 是每请求最多 20 张、无持久任务/自动重试/取消语义的同步兼容入口；该受限例外不得扩展为新的请求内 embedding 入口。
- 不在应用启动、普通部署或健康检查中隐式运行兼容审计或迁移。
- 永久清除对象 final manifest 只是 `backup_only_no_delete` 备份证据，不是删除授权；正式删除前仍须在锁或写入 fence 内重新验证全部引用来源和正式对象身份。
- 不为对象备份、隔离恢复或备份 Bucket 暴露 Delete；partial/orphan、正式对象和隔离对象都不得由本仓库自动清理。
- Legacy TypeScript 组件（如 OrderManagement）未路由且可能有类型错误，除非重新启用，否则不要顺手修复。
- 如果 Docker 报告历史容器名称冲突，只处理明确冲突的容器；不要删除数据库卷。
- 完成稳定功能后，仅在改变架构事实、入口或操作约束时更新最近作用域的 AGENTS.md；记录当前事实，不记录实现过程。

## Agent 工作流与模型路由

### 工作流事实源

- Issues 和 PRD 使用 GitHub Issues（richardzhangy26/xiangyi_image_search），通过 gh CLI 操作；见 docs/agents/issue-tracker.md。
- 默认 triage 标签为 needs-triage / needs-info / ready-for-agent / ready-for-human / wontfix；见 docs/agents/triage-labels.md。
- 长期领域事实以根目录 CONTEXT.md 和 docs/adr/ 为准；见 docs/agents/domain.md。GitHub Issue 记录交付范围，Superpowers plan 只是单张高风险 Ticket 的短期执行配方。

### Codex 技能调用

- 在 Codex 中使用 `$skill-name` 显式调用技能，或通过 `/skills` 选择；不要把 Matt 文档中的通用 `/skill` 写法误当成 Codex 命令。
- `$ask-matt` 必须由用户显式调用，并且只负责在 Matt skills 内选路，不负责选择 Matt 与 Superpowers 两条总工作流。
- 对功能、修复或重构，在写代码前确定 `lane=matt` 或 `lane=superpowers`；纯解释、只读调查和代码审查不需要强行指定 lane。

### Lane 选择

- 默认使用 `lane=matt`：普通前端功能、常规 Flask CRUD、CSV 校验、文档、小型 Bug，以及不改变存储、事务或 schema 语义的改动。
- 以下情况使用 `lane=superpowers`：schema、事务、并发、OSS/Kodo、pgvector、embedding 模型或维度、权限、legacy 数据、数据迁移、恢复点、真实外部写入，或跨多个边界且失败代价高的改动。
- Matt 可以先产出整体 spec 和 tickets；其中某张高风险 Ticket 只能在 Ticket 边界进入 Superpowers。执行中若范围升级为高风险，立即停止，重新确认 lane 和授权，不得在同一执行上下文中悄悄换流程。
- 一张 Ticket 只能有一个 lane、一个 TDD 流程、一个诊断流程和一个主 review 流程；不得叠加两套完整工作流。

### Matt lane

- 不确定该用哪个 Matt skill 时，由用户显式调用 `$ask-matt`。
- 一般主线为 `$grill-with-docs` → 必要时 `$prototype` → 多会话任务 `$to-spec` → `$to-tickets` → 每张 Ticket 在新任务中 `$implement` → `$code-review`。
- 能在当前上下文完成的小改可直接 `$implement`；困难 Bug 使用 `$diagnosing-bugs`；外部资料调查使用 `$research`。
- 从需求访谈到 `$to-tickets` 保持同一上下文；每张 `$implement` Ticket 使用新的 Codex 任务。Matt lane 不再调用 Superpowers 的 brainstorming、writing-plans 或 subagent-driven-development。

### Superpowers lane

- 主线为 `$brainstorming` → `$writing-plans` → `$using-git-worktrees` → `$subagent-driven-development`（或 `$executing-plans`）→ `$verification-before-completion` → `$finishing-a-development-branch`。
- 已有 Matt spec 或 Ticket 时，将它作为 Superpowers 的输入并只确认该 Ticket 的设计，不重新访谈、拆分或实现整个上游功能。
- 写计划前必须由 `architect` 审查架构边界、不变量、失败模式、回滚与测试接缝；实现完成后必须由 `risk_reviewer` 对完整 diff 做独立审查。
- Superpowers lane 不再调用 Matt 的 `$to-spec`、`$to-tickets`、`$implement` 或 `$code-review`。`$verification-before-completion` 可作为两条 lane 共用的完成门，但不是第二套 review。

### 模型分工

| 角色 | 模型 | 使用范围 |
| --- | --- | --- |
| 主线程 | Terra Medium | 普通分析、Matt 工作流、计划落盘、常规实现与协调 |
| `explorer` | Luna Medium，只读 | 代码搜索、调用链、测试定位和证据收集 |
| `luna_worker` | Luna Medium | 验收条件完整、范围不超过 1–2 个文件的机械修改和预批准命令 |
| Terra worker / 普通 reviewer | Terra | 多文件集成、Matt prose Ticket 实现、普通 `/review` 与 `$code-review` |
| `architect` | Sol XHigh，只读 | 高风险设计与实施计划审查，不直接修改文件 |
| `risk_reviewer` | Sol High，只读 | 安全、事务、迁移和高风险完整 diff 的最终独立审查 |

- 便宜模型对同一验收目标连续失败两次，或任务需要自行补齐设计判断时，立即升级到 Terra；涉及数据安全、不可逆操作或跨边界契约冲突时升级到 `architect`，不得无限重试。
- 默认最多并行三个子代理。优先并行只读探索、独立 review 和报告整理；写入范围重叠或任务存在依赖时禁止并行实现。

### 共享授权门

- 模型或 lane 选择不构成迁移、部署、删除、真实 OSS/Kodo 写入、数据库收缩、创建或修改 GitHub Issues、commit、push 或 PR 的授权。
- 技能内置的发布、提交或合并步骤不得覆盖用户授权边界；执行外部或 Git 操作前确认用户是否明确要求。
- 所有代理必须保护现有未提交改动，只修改被授权的文件；需要扩大范围时先报告原因和最小必要范围。
