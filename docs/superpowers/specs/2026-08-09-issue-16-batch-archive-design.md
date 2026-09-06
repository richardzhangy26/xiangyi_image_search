# Issue #16 批量移入回收站设计

- 日期：2026-08-09
- 状态：基于 Issue #14、Issue #16、CONTEXT.md、ADR-0005/0006 与已移植 Issue #15 规格的实施设计
- 基线：`refactor/image-search-pgvector@088bb9f` 加 Issue #15 全部未提交 delta
- 范围：只对活跃、未归款图片资产执行最多 100 项的原子批量归档，并从所有发现型入口隐藏

## 1. 架构审查结论

新增一个深模块 `services.asset_archive`，公开单一命令接口：

```python
archive_unassigned_image_assets(
    session,
    asset_ids: object,
    *,
    request_id: str,
) -> ArchiveBatchResult
```

HTTP 蓝图只负责 JSON、请求标识与 HTTP 状态码适配。事务模块负责结构校验、稳定顺序行锁、全批业务判定、条件更新、版本递增、批次与逐资产活动记录以及唯一一次提交或回滚。这样调用方只需理解一个接口，复杂性不会散落到蓝图、查询或前端。

PostgreSQL 是本地可替代依赖；生产使用 SQLAlchemy session，纯单元测试使用 fake session。本轮禁止连接任何 PostgreSQL，因此真实事务、锁和 pgvector 场景只编写在 `test/integration`，不执行。

## 2. 批量命令不变量

- 请求必须包含 1 至 100 个字符串 UUID；解析后的重复 UUID 整批冲突。
- `active + model_number IS NULL` 是唯一可发生状态变更的目标。
- `archived + model_number IS NULL` 是合法幂等重试：不再次修改 `version`、`archived_at` 或其他资产字段。
- 已归款、缺失、未知生命周期状态、重复 ID 均使整批资产保持不变，并返回逐项原因。
- `archived + model_number IS NOT NULL` 不是本接口产生的状态，按“只限未归款”约束拒绝，不能借幂等语义绕过。
- 活跃与已归档未归款目标可以混合：活跃项归档，已归档项 no-op，整批成功。
- 所有目标按 UUID 稳定排序后 `SELECT ... FOR UPDATE`。完成全批判定前不更新任何资产。
- 既有批量归款入口也按 UUID 升序获取行锁，保证 archive/assign 对重叠批次使用同一锁顺序，降低反序死锁风险。
- 对符合条件的活跃目标执行一条条件 `UPDATE ... RETURNING`，只设置 `status='archived'`、`archived_at=NOW()`、`updated_at=NOW()` 与 `version=version+1`。
- 成功状态变更、一个批次活动记录和每个目标一个活动记录在同一事务提交；任一活动记录失败必须回滚资产更新。
- 幂等重试仍写本次请求的 batch 记录和 `noop` 逐资产结果，这是审计要求，不属于重复生命周期副作用。
- 不修改资产 ID、型号、来源身份、展示名称、向量、embedding 元数据、OSS 原图键或预览键；不调用对象存储或 embedding。

## 3. 外部合同

接口：`POST /api/image-assets/archive`

请求：

```json
{
  "asset_ids": [
    "7e4f76b5-f771-4e36-a035-33c32aa2654f",
    "28a5262b-fd4b-4b61-b321-875572f4d42f"
  ]
}
```

成功响应：

```json
{
  "batch_id": "0d30c337-4f47-430b-bdc4-6f5eab226ba4",
  "status": "succeeded",
  "archived_count": 1,
  "already_archived_count": 1,
  "items": [
    {"asset_id": "7e4f76b5-f771-4e36-a035-33c32aa2654f", "status": "archived", "version": 2},
    {"asset_id": "28a5262b-fd4b-4b61-b321-875572f4d42f", "status": "already_archived", "version": 4}
  ]
}
```

错误模式：

- 400 `INVALID_IMAGE_ASSET_ARCHIVE_BATCH`：非数组、空批次、超过 100、非字符串或非法 UUID；不持久化未经验证的原始输入。
- 409 `IMAGE_ASSET_ARCHIVE_CONFLICT`：重复、缺失、已归款或非法状态；返回 `batch_id` 和逐项稳定错误码，资产全批不变。重复 UUID 已安全规范化，因此可记录被拒绝的 batch 与唯一目标结果。
- 500 `IMAGE_ASSET_ARCHIVE_FAILED`：数据库、活动记录或提交失败；统一脱敏并回滚。

逐项状态只使用 `archived`、`already_archived`、`unchanged`、`rejected`。事务型失败不返回伪部分成功。

## 4. 活动记录

复用 Issue #15 的 `asset_activity_records`，不新增 schema：

- batch：`event_type='asset.archive.batch'`、`target_type='image_asset_batch'`、`target_id=batch_id`、`batch_id=batch_id`。
- item：`event_type='asset.archive'`、`target_type='image_asset'`、`target_id=asset_id`、`batch_id=batch_id`。
- batch 状态摘要只保存请求数量和结果计数；item 状态摘要只保存型号、显示名称、版本和生命周期状态。
- 不保存凭证、签名 URL、OSS 对象内容、图片内容或向量。

部署前置条件是先显式执行并验证 Issue #15 migration；#16 不增加迁移，也不在启动、健康检查或普通部署中隐式执行迁移。

## 5. 服务端可见性审计

当前发现型入口已显式过滤 active，本 Ticket 用静态合同固定这些事实：

- `GET /api/image-assets` 的默认列表、归款筛选、显示名称/来源路径搜索与分页；
- `VectorSearchService` 的 pgvector 原生 SQL；
- 产品列表和产品详情中的图片集合；
- 图片统计；
- 归款候选列表与归款写入口的状态校验。

归款写入口的 `FOR UPDATE` 同时增加显式 `ORDER BY ImageAsset.id`；这不改变归款业务语义，只统一跨命令锁顺序。

预览入口是已知资产 ID 的私有解引用，不是发现入口。它必须继续允许 archived，以便后续回收站查看和恢复；签名 URL 仍为短时私有 302。

## 6. 前端流程

`ProductUpload` 继续拥有选择、请求、toast、刷新和确认状态；`UnassignedAssetGrid` 只展示选择工具栏并上报用户意图。

- 没有选择时不渲染批量工具栏。
- 只有未归款卡片可选择；不扩大到已归款资产。
- 选择后同时显示既有关联型号动作与“移入回收站”。
- 确认文案明确说明图片会从普通搜索和向量搜索消失，但不删除原图、预览或向量，并可在回收站恢复。
- 成功后清空选择、关闭确认框并刷新当前资产页；失败时保留选择和确认状态并显示后端可读原因。
- 当前每页 24 张且翻页/搜索/筛选会清空选择，因此前端不会形成超过 100 项的请求；100 项上限仍由服务端最终执行。

## 7. 测试 seam 与未执行项

已确认的测试 seam：

1. `archive_unassigned_image_assets` 公开接口：fake session 验证校验、幂等、冲突、条件更新、审计与 rollback。
2. Flask 外部接口 + 独立真实 PostgreSQL：验证 HTTP、行锁事务、持久字段、活动记录与 pgvector 排除；只编写并标注，当前不执行。
3. `ProductUpload` 顶层用户流程：React Testing Library + fake API 覆盖选择、确认、取消、成功、失败和刷新。
4. `productApi` transport：固定 endpoint、method、body 和服务端错误透传。

静态合同验证所有 active 谓词与“归档模块不包含删除/OSS/embedding 路径”。它不能替代真实 PostgreSQL 对事务隔离、`FOR UPDATE`、`NOW()`、pgvector 或回滚的验证。

## 8. 已知阶段性风险与边界

- 既有产品单图归档入口可归档已归款资产，但没有版本递增和活动记录；它不属于 #16 的未归款批量入口，不能在本 Ticket 中顺手改变产品语义。
- 既有同源产品重传会静默重新激活 archived；这与 Issue #14 最终语义冲突，已由后续可靠导入/回收站 Ticket 负责。本 Ticket 不扩张到上传流程，但最终报告必须注明该旁路仍存在。
- 真实 PostgreSQL 测试未运行时，不能宣称事务、锁、pgvector 或持久审计语义已经通过。
- 本 Ticket 不实现回收站列表、恢复、永久清除、自动过期、OSS 删除、真实迁移或部署。
