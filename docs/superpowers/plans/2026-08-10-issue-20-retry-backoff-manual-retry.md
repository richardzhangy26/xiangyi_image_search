# Issue #20 自动退避重试、可诊断失败与手工重试 实施计划（lane=superpowers）

> 输入：GitHub Issue #20 验收标准 + /tmp/architect-review-issue-20.md（architect 审查结论）。
> 基线：本 worktree 的 #15–#19 累计 delta（父门禁 119 + 53 全绿）。

**Goal:** 让暂时性 embedding 故障自动恢复（持久指数退避，最多 5 次尝试），确定性错误快速失败并可诊断，耗尽后用户可手工重试且复用已上传对象；全过程持久、幂等、多 worker 安全，不创建不合格资产。

**Architecture:** 在 #19 持久队列上新增 `awaiting_retry` 状态与调度字段；失败分类为纯函数；等待表达为持久 `next_retry_at` + 领取过滤（worker 绝不进程内 sleep 退避）；手工重试只是「条件 UPDATE 重新入队」。

## Global Constraints

- 模型/维度契约不变：`tongyi-embedding-vision-plus-2026-03-06` / 1024；失败项绝不产生正式/空/占位向量。
- 自动尝试总数最多 5 次；确定性失败（图片不可处理、模型不兼容、向量格式/维度错误、预览对象缺失）不自动重试。
- 可靠状态只在 PostgreSQL；无请求线程/内存队列权威；worker 无退避 sleep。
- 迁移 expand-only、幂等、显式 --apply，不被 app/health/worker 隐式导入。
- 禁止引入 #21 范围（cancel 状态、取消 API/UI、迟到结果处理）。
- 不连接真实 PostgreSQL/OSS/Kodo/DashScope；集成场景只写不执行。
- 不 commit/push/PR/改 GitHub/部署。

## File Map

| 文件 | 职责 |
|---|---|
| `backend/services/import_retry.py`（新建） | 错误分类纯函数、退避计算、尝试上限常量、错误类常量 |
| `backend/models/image_import_item.py` | 新增 attempt_count/last_error_class/last_attempt_at/next_retry_at 与 awaiting_retry 状态 |
| `backend/migrations/issue_20_retry_backoff.py`（新建） | 显式、幂等、expand-only 迁移 |
| `postgres/init/01_init.sql` | 新装 schema 与 ORM/迁移保持一致 |
| `backend/services/embedding.py` | 结构化异常：网络错误/5xx 服务端错误子类（不改既有行为语义） |
| `backend/services/object_storage.py` | download_file 附带 stage/status_code 结构化信号 |
| `backend/services/image_import_worker.py` | 领取含到期 awaiting_retry；失败路径分类→重试调度或失败 |
| `backend/blueprints/image_imports.py` | 手工重试端点 + 列表计数含 awaiting_retry |
| `frontend/src/types/product.ts` | awaiting_retry 状态与新字段契约 |
| `frontend/src/services/productApi.ts` | retryImageImportItem 传输 |
| `frontend/src/components/ImageImportTaskDrawer.tsx` | 等待重试展示、失败摘要与手工重试按钮 |
| `frontend/src/components/ProductUpload.tsx` | 手工重试编排与刷新 |
| 测试（新建） | test_issue_20_retry_policy_unit.py / test_issue_20_worker_retry_unit.py / test_issue_20_schema_static_contract.py / test_issue_20_api_static_contract.py / test_issue_20_api_retry_unit.py / 前端 Drawer 与 ProductUpload 扩展测试 |

## 关键设计决定

1. **状态机**：`awaiting_retry` 独立状态。领取条件 = `asset_id IS NULL` 且（queued 或 embedding 租约过期 或 `awaiting_retry AND next_retry_at <= now`）。领取时 `attempt_count += 1`（崩溃接管再领取同样计数，防无限崩溃循环），并清 next_retry_at。
2. **错误分类**（纯函数 `classify_import_failure(exc)`，禁止解析消息字符串）：
   - `EmbeddingRateLimitExhaustedError` → `rate_limited`（可重试）
   - `EmbeddingNetworkError`（新增）→ `network`（可重试）
   - `EmbeddingServerError`（新增，5xx）→ `server_error`（可重试）
   - `ObjectStorageError`：status_code==404 → `storage_missing`（确定性）；其他下载失败 → `transient_storage`（可重试）
   - `InvalidEmbeddingResult` → `embedding_incompatible`（确定性）
   - 其余 `EmbeddingServiceError`（坏图解码/4xx 等）→ `deterministic_request`（确定性）
   - 其他未知异常 → `unknown`（确定性；保守不自动重试，手工重试兜底——保持 #19 父测试的失败路径语义不变）
3. **退避**：`next_retry_delay_seconds(attempt_count) = min(cap, base × 2^(attempt_count-1))`；base=30s（env `IMAGE_IMPORT_RETRY_BASE_SECONDS` 可配），cap=3600s（env `IMAGE_IMPORT_RETRY_CAP_SECONDS`）。`should_auto_retry(error_class, attempt_count) = 类可重试 且 attempt_count < 5`。
4. **失败路径**：可重试且未耗尽 → `schedule_import_retry(...)`：status='awaiting_retry'、next_retry_at=now+delay、last_error_class、failure_message（沿用 `处理失败（类型名）` 消毒格式）、清租约字段、activity `image_import.awaiting_retry`；否则 → `mark_import_item_failed`（after_state 含 error_class），activity 不变。
5. **手工重试**：`POST /api/image-imports/<uuid:item_id>/retry`。行锁内条件转移：failed/awaiting_retry → awaiting_retry 且 next_retry_at=now（立即可领取）、清租约字段、activity `image_import.manual_retry`（source='api'）；queued/embedding → 200 当前状态（幂等）；completed → 409 `IMAGE_IMPORT_RETRY_COMPLETED`。不重置 attempt_count。
6. **响应面**：`to_public_dict` 增加 attempt_count、max_auto_attempts、last_error_class、last_attempt_at、next_retry_at（不暴露 claim/OSS 键/向量）。`unresolved_count` 含 awaiting_retry；`processing_count` 保持 queued+embedding。
7. **被取代的 #19 静态合同更新**（遵循 #19 计划处理 #18 禁令的先例）：
   - schema 合同的状态字符串断言升级为五状态超集；
   - worker 合同的 `retry_count/next_attempt/backoff` 禁令改为允许 #20 重试字段、仍禁止 cancel/delete/placeholder；
   - API 合同的 `'retry' not in` 禁令改为允许 #20 重试端点、仍禁止 cancel。
   所有其他断言保持不动。

## Tasks

- [ ] Task 1：重试策略纯函数与分类器（RED→GREEN，test_issue_20_retry_policy_unit.py）
- [ ] Task 2：schema/ORM/迁移/01_init 扩展 + 静态合同（含被取代 #19 断言更新）
- [ ] Task 3：embedding/object_storage 结构化异常（不破坏父门禁）
- [ ] Task 4：worker 领取与失败重试链路（RED→GREEN，test_issue_20_worker_retry_unit.py）
- [ ] Task 5：手工重试 API + 静态/单元测试
- [ ] Task 6：前端状态、抽屉与编排（静态导入；RED→GREEN）
- [ ] Task 7：全门禁（父 16+7、新增测试、build、compileall、compose config、git 检查）+ 安全扫描 + 报告

## 未执行项（保持未执行）

真实 PostgreSQL 的迁移执行、退避到期的多 worker 领取竞争、端到端排队→退避→重试→向量检索。集成测试文件只写不执行。
