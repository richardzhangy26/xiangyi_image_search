# Issue #17 回收站浏览、搜索与无损批量恢复设计

- 日期：2026-08-09
- 状态：基于 Issue #14、Issue #17、CONTEXT.md、ADR-0005/0006 与已移植 Issue #15/#16 累计实现的实施设计
- 基线：`refactor/image-search-pgvector@088bb9f` 加 Issue #15/#16 全部未提交 delta
- 范围：独立回收站列表、数量、双字段搜索、私有预览与最多 100 项的原子无损恢复

## 1. 架构审查结论

新增深模块 `services.asset_recycle_bin`，公开两个入口：

```python
list_archived_image_assets(
    session,
    *,
    page: int,
    per_page: int,
    search: str,
) -> ArchivedAssetPage

restore_image_assets(
    session,
    asset_ids: object,
    *,
    request_id: str,
) -> RestoreBatchResult
```

Flask 蓝图只负责 query/JSON、请求标识及 HTTP 状态适配。模块隐藏 archived 查询、未筛选数量、双字段字面量搜索、稳定排序、UUID 规范化、行锁、全批状态判定、条件更新、活动记录以及唯一一次提交或回滚。

PostgreSQL/SQLAlchemy 是本地可替代依赖。生产使用 SQLAlchemy session；本轮纯单元测试使用 fake session。真实 PostgreSQL 的分页、`FOR UPDATE`、事务、回滚和 pgvector 可见性场景只编写，不执行。

不把回收站塞入既有 active 列表的 `assignment` 维度，也不新增通用生命周期状态机、count 路由、排序索引或 schema migration。这样不削弱 Issue #16 固定的所有发现型入口 `status='active'` 约束。

## 2. 回收站只读列表

接口：

```http
GET /api/image-assets/archived?page=1&per_page=24&search=挂绳
```

固定规则：

- 只查询 `status='archived'`；已归款归档资产仍可见，用于解释既有产品单图归档旁路，但前端禁用其批量恢复选择。
- `page` 为 1 至 1,000,000，`per_page` 为 1 至 100；非法参数返回 400 `INVALID_IMAGE_ASSET_ARCHIVED_LIST_PARAMS`。
- `search` 去除首尾空白后，同时以字面量、大小写不敏感的包含匹配查询 `display_name` 与 `source_relative_path`；`\`、`%`、`_` 继续转义。
- 排序固定为 `archived_at DESC NULLS LAST, id DESC`，不向调用方暴露可配置排序。
- `total` 是当前搜索条件下的数量；`archived_total` 是未受搜索影响的全部 archived 数量，用于回收站标签。
- 安全表示复用管理资产 DTO 并增加 `archived_at`；预览仍为 `/api/image-assets/<asset_id>/preview`。
- 列表不提交事务，不改名、不归款、不恢复、不修复历史数据。

响应：

```json
{
  "assets": [{
    "asset_id": "7e4f76b5-f771-4e36-a035-33c32aa2654f",
    "model_number": null,
    "display_name": "蓝色挂绳.png",
    "source_relative_path": "挂绳/A47/2.png",
    "version": 3,
    "status": "archived",
    "archived_at": "2026-08-09T12:00:00",
    "preview_url": "/api/image-assets/7e4f76b5-f771-4e36-a035-33c32aa2654f/preview"
  }],
  "total": 1,
  "archived_total": 37,
  "page": 1,
  "per_page": 24
}
```

## 3. 批量恢复状态矩阵

接口：

```http
POST /api/image-assets/restore
Content-Type: application/json

{"asset_ids": ["uuid-1", "uuid-2"]}
```

请求必须包含 1 至 100 个字符串 UUID。规范化后的重复 UUID、缺失资产、未知生命周期状态，以及 `archived + model_number IS NOT NULL` 均使整批资产保持不变。

| 锁定时状态 | 处理 | 说明 |
|---|---|---|
| `archived + model_number IS NULL` | `restored` | 真正恢复，版本递增一次 |
| `active + model_number IS NULL` | `already_active` | 幂等 no-op |
| `active + model_number IS NOT NULL` | `already_active` | 幂等 no-op；保留恢复后并发发生的归款，不得暗中解绑 |
| `archived + model_number IS NOT NULL` | `rejected` | 不属于未归款回收站恢复，整批拒绝 |
| 未知/未来清除状态 | `rejected` | 白名单 fail-closed，整批拒绝 |
| 缺失或重复 | `rejected` | 整批拒绝 |

真正恢复执行单条条件 `UPDATE ... RETURNING`，仅写：

- `status='active'`
- `archived_at=NULL`
- `updated_at=NOW()`
- `version=version+1`

条件仍包含 `status='archived' AND model_number IS NULL`。更新数与预期不一致必须回滚。`model_number` 不参与写集合，因此真正恢复的资产仍为 NULL；资产 ID、显示名称、来源身份、向量、embedding 元数据、OSS 原图/预览绑定、内容元数据和 `created_at` 均保持不变。

所有目标按 UUID 升序 `SELECT ... FOR UPDATE`，但逐项响应保持请求首次出现顺序。恢复后既有 active 谓词自然让资产重新进入普通搜索、向量搜索和待归款列表，不上传对象、不重新生成预览或 embedding。

## 4. 响应与错误

成功响应：

```json
{
  "batch_id": "0d30c337-4f47-430b-bdc4-6f5eab226ba4",
  "status": "succeeded",
  "restored_count": 1,
  "already_active_count": 1,
  "items": [
    {"asset_id": "uuid-1", "status": "restored", "version": 4},
    {"asset_id": "uuid-2", "status": "already_active", "version": 7}
  ]
}
```

错误模式：

- 400 `INVALID_IMAGE_ASSET_RESTORE_BATCH`：非数组、空、超过 100、非字符串或非法 UUID；不审计无稳定目标的原始输入。
- 409 `IMAGE_ASSET_RESTORE_CONFLICT`：重复、缺失、已归款归档或非法状态；返回 batch ID 与逐项 `error_code`、可读 `error`，资产全批不变。
- 500 `IMAGE_ASSET_RESTORE_FAILED`：锁、更新、活动记录或提交失败；统一脱敏并 rollback。

逐项状态固定为 `restored`、`already_active`、`unchanged`、`rejected`。事务型失败不返回伪部分成功。

## 5. 活动记录

复用 Issue #15 的 `asset_activity_records`，不新增 schema：

- batch：`event_type='asset.restore.batch'`、`target_type='image_asset_batch'`。
- item：`event_type='asset.restore'`、`target_type='image_asset'`。
- active 幂等项记录为 `noop`；冲突批次记录拒绝原因但不修改资产。
- batch 摘要只保存请求数和结果计数；item 摘要继续使用 `activity_state` 的型号、显示名称、版本和生命周期状态。
- 不记录凭证、签名 URL、OSS 对象内容、图片内容或完整向量。

恢复变更与 batch/item 活动记录在同一事务提交；活动记录失败必须回滚全部恢复。

## 6. 前端流程

`ProductUpload` 顶层视图扩展为“图片资产 / 回收站 / 产品资料”。首次加载 active 页与 archived 页，以 `archived_total` 展示独立回收站数量。

新增 `ArchivedAssetGrid`：

- 双字段搜索、分页、错误重试与私有预览。
- 显示名称只读，不渲染改名编辑器。
- 卡片展示显示名称、来源路径、归档时间和必要的型号提示。
- 只有未归款 archived 项可选择；已归款 archived 项可见但明确不可恢复。
- 选中后显示“恢复选中图片”；成功清空选择并同时刷新回收站与 active 页，失败保留选择并展示服务端可读原因。
- 不显示永久清除、自动过期、重新上传或重新 embedding 操作。

归档成功同样刷新 archived 页，使标签数量同步更新。

## 7. 测试 seam 与安全边界

允许执行：

1. `asset_recycle_bin` fake-session 单元测试：参数、列表表示、双计数、恢复、active 幂等、冲突、更新数校验、活动失败、commit/rollback 与安全状态摘要。
2. 静态合同：独立 archived 路由、双字段搜索、固定排序、恢复写集合、active 发现过滤、无 delete/OSS/embedding/过期/永久清除路径。
3. `productApi` fetch stub、`ArchivedAssetGrid` 组件测试与 `ProductUpload` 顶层 fake-API 流程。
4. TypeScript build 与 `git diff --check`。

只编写、不执行：真实 PostgreSQL HTTP/事务/行锁/回滚/pgvector 场景。禁止加载数据库或云凭证，禁止连接 PostgreSQL、OSS、Kodo、DashScope，禁止迁移、部署、删除、commit、push 或修改 GitHub Issue/PR。

## 8. 明确不实施

- 自动过期、永久清除、purged 状态、tombstone、清除按钮或清除接口。
- OSS 原图/预览复制、覆盖、删除或重新上传。
- 预览重建、embedding 重算或向量改写。
- 修改产品单图归档语义。
- 修复既有同源产品重传静默重新激活 archived 的旁路；它仍由后续可靠导入 Ticket 处理。
