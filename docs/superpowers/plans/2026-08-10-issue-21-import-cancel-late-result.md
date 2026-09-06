# Issue #21 导入任务取消与迟到结果防护 实施计划（lane=superpowers）

> 输入：GitHub Issue #21 验收标准 + /tmp/architect-review-issue-21.md（architect 审查结论）。
> 基线：本 worktree 的 #15–#19 累计 delta（父门禁全绿）。与 #20 并行、互不引入对方范围；#22 负责汇合。

**Goal:** 让图库管理员对尚未形成正式资产的导入项单项/批量取消；取消意图持久且幂等；worker 在三个竞争窗口都检查取消状态，迟到 embedding 结果被丢弃且不创建资产；已完成项拒绝取消并引导回收站。

**Architecture:** 「取消意图时间戳 `cancel_requested_at` + 终态 `cancelled`」双层模型。意图先行写入不干扰 claim 栅栏；worker 检查点与清扫路径负责落终态。所有竞争方经同一行 `SELECT ... FOR UPDATE` 串行化——意图提交后不可能再创建资产。

## Global Constraints

- 安全边界：取消不删除暂存 OSS 对象；不覆盖、不公开 URL。
- 模型/维度契约不变；取消项绝不产生正式/空/占位向量。
- 可靠状态只在 PostgreSQL；无请求线程/内存队列权威。
- 迁移 expand-only、幂等、显式 --apply，不被 app/health/worker 隐式导入。
- 禁止引入 #20 范围（awaiting_retry 状态、attempt_count、退避/错误分类、手工重试 API/UI）。
- 不连接真实 PostgreSQL/OSS/Kodo/DashScope；三窗口并发集成场景只写不执行。
- 不 commit/push/PR/改 GitHub/部署。

## File Map

| 文件 | 职责 |
|---|---|
| `backend/models/image_import_item.py` | cancel_requested_at/cancel_requested_by/cancelled_at 字段、cancelled 状态、CANCELABLE_STATUSES 常量 |
| `backend/migrations/issue_21_import_cancel.py`（新建） | 显式、幂等、expand-only 迁移 |
| `postgres/init/01_init.sql` | 新装 schema 与 ORM/迁移一致 |
| `backend/services/image_import_worker.py` | 领取排除取消意图；调用前/提交前检查点；迟到结果丢弃；清扫路径 |
| `backend/blueprints/image_imports.py` | 单项/批量取消端点（幂等、逐项结果、上限 100） |
| `frontend/src/types/product.ts` | cancelled 状态与取消字段契约 |
| `frontend/src/services/productApi.ts` | cancelImageImportItem / cancelImageImports 传输 |
| `frontend/src/components/ImageImportTaskDrawer.tsx` | 选择、批量取消、确认、逐项结果 |
| `frontend/src/components/ProductUpload.tsx` | 取消编排与刷新 |
| 测试（新建/扩展） | test_issue_21_cancel_unit.py / test_issue_21_worker_cancel_unit.py / test_issue_21_schema_static_contract.py / test_issue_21_api_static_contract.py / test_issue_21_api_cancel_unit.py / test_issue_21_integration_concurrency.py（只写不执行）/ 前端抽屉与编排扩展测试 |

## 关键设计决定

1. **取消意图建模**：新增列 `cancel_requested_at TIMESTAMP`、`cancel_requested_by VARCHAR(128)`、`cancelled_at TIMESTAMP`；status 超集加 `cancelled`。意图可对任何可取消行写入；终态由 worker 检查点/清扫落定。
2. **CANCELABLE_STATUSES**：`('queued', 'embedding', 'failed')`（本分支无 awaiting_retry，#22 汇合时并入）。completed 拒绝。
3. **领取过滤**：claim WHERE 追加 `cancel_requested_at IS NULL`；cancelled 不在领取状态集 → 不可领取。
4. **三个竞争窗口**（同一行 FOR UPDATE 串行化）：
   - 调用前：worker 领取提交后、调用 embedding 前查意图 → 落 cancelled，不调用。
   - 返回后/提交前：`complete_import_item` 校验追加「意图必须为空」；有意图 → `discard_late_result` 丢弃，不建资产。
   - `mark_import_item_failed` 先查意图：有意图 → cancelled 而非 failed。
5. **清扫路径**（防 embedding+意图僵尸行）：worker 每轮把 `cancel_requested_at IS NOT NULL` 且处于 queued/failed（无条件）或 embedding 租约过期 的行落 cancelled + activity。
6. **批量取消 API**：`POST /api/image-imports/<id>/cancel` 单项 + `POST /api/image-imports/cancel` 批量（body item_ids，上限 100）；逐项结果 `cancelled | already_cancelled | completed_rejected | not_found`；整体 200；activity 共享 batch_id。
7. **计数语义**：cancelled 为已解决终态，不计入 unresolved_count。
8. **回滚窗口**：计划与文档写明部署顺序（先升级 worker 再开放取消 API），旧 worker 不读意图。

## Tasks

- [ ] Task 1：schema/ORM/迁移/01_init 扩展 + 静态合同
- [ ] Task 2：worker 领取过滤、调用前检查点、迟到结果丢弃、failed 意图优先、清扫路径（RED→GREEN）
- [ ] Task 3：单项/批量取消 API + 静态/单元测试
- [ ] Task 4：三窗口真实 PostgreSQL 集成测试（只写不执行）
- [ ] Task 5：前端状态、抽屉选择/批量取消/确认/逐项结果与编排（静态导入；RED→GREEN）
- [ ] Task 6：全门禁（父 16+7、新增测试、build、compileall、compose config、git 检查）+ 安全扫描 + 报告

## 未执行项（保持未执行）

真实 PostgreSQL 的三窗口并发（取消 vs 领取 / 调用返回 / 资产提交）、迁移执行、端到端。集成测试只写不执行。
