# Issue #22 导入项保留期、恢复、放弃与引用安全清理 实施计划（lane=superpowers）

> 基线：#22 worktree（#20+#21 已汇合，六状态机、重试与取消并存，门禁 190+62 全绿）。
> 安全边界：真实 OSS 清理与生产定时任务启用需单独授权；本计划只允许伪 OSS 测试，绝不调用真实云删除。

**Goal:** 取消项保留 7 天、重试耗尽失败项保留 30 天，窗口内可恢复导入；用户可提前放弃（不可逆提示）；到期后由可重启、幂等、持久检查点的清理任务按引用安全规则删除暂存对象；回收站资产永不受影响。

## Global Constraints

- 新增状态 `abandoned`（终态）；状态机扩展为七状态超集。
- 保留窗口：cancelled → 7 天（`purge_eligible_at = cancelled_at + 7d`）；failed（耗尽）→ 30 天；abandoned → 立即到期。手工重试 failed 项清空 `purge_eligible_at`（下次失败重新计算），满足「手工重试重新计算 30 天」。
- 清理对象引用规则：
  - 原图 `oss_path`：无任何 `image_assets.oss_path`（active+archived）引用，且无其他 `objects_purged_at IS NULL` 的导入项引用时才删除。
  - 预览 `preview_oss_path`：同规则；归档（回收站）资产的引用永远保护对象。
- 清理任务按项推进：每项一个检查点（`objects_purged_at`），可重启续跑；对象删除幂等（对象已不存在视为成功）。
- 清理失败不置 purged 标记，下次运行重试。
- 清理入口默认不随部署启动（compose profile 隔离 + env 开关）。
- 不连接真实 OSS/PostgreSQL/DashScope；删除类测试全部使用伪存储。

## File Map

| 文件 | 职责 |
|---|---|
| `backend/services/import_retention.py`（新建） | 保留期纯函数：窗口计算、到期判定、剩余时长 |
| `backend/models/image_import_item.py` | abandoned 状态、purge_eligible_at、objects_purged_at 字段与响应 |
| `backend/migrations/issue_22_retention_cleanup.py`（新建） | 幂等 expand-only 迁移（列 + 七状态约束重建） |
| `postgres/init/01_init.sql` | 新装 schema 同步终态 |
| `backend/services/object_storage.py` | `delete_object(key)` 结构化删除适配（NoSuchKey=成功） |
| `backend/services/import_cleanup.py`（新建） | 引用计数 + 逐项清理 + 活动记录 |
| `backend/services/image_import_worker.py` | failed/cancelled 转移时写 purge_eligible_at |
| `backend/blueprints/image_imports.py` | restore/abandon 端点；retry 清空 purge_eligible_at；响应窗口字段 |
| `backend/scripts/run_import_cleanup.py`（新建） | 独立清理进程（SIGTERM 优雅停止，env 门控） |
| `docker-compose.yml` | cleanup 服务（profile 隔离，默认不启动） |
| 前端 types/productApi/Drawer/ProductUpload | 剩余窗口展示、恢复、放弃确认弹窗、编排 |
| 测试 | retention 纯函数、cleanup 引用矩阵、restore/abandon API、静态合同、前端 |

## Tasks

- [ ] Task 1：retention 纯函数 + 静态/单元测试（RED→GREEN）
- [ ] Task 2：schema/ORM/迁移/init（abandoned、purge_eligible_at、objects_purged_at、七状态）
- [ ] Task 3：worker/API 在状态转移时写窗口字段；retry 重置窗口
- [ ] Task 4：object_storage.delete_object + cleanup 引用安全服务（RED→GREEN，伪存储）
- [ ] Task 5：restore/abandon API + 单元/静态测试
- [ ] Task 6：清理进程脚本 + compose profile + 静态合同
- [ ] Task 7：前端窗口展示/恢复/放弃（RED→GREEN）
- [ ] Task 8：全门禁 + 安全扫描 + 报告

## 真实环境未执行项

真实 PostgreSQL 的到期扫描并发、真实 OSS 删除、compose 启动清理服务——只写不执行。
