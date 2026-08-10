# Issue #15 图片资产显示名称与乐观并发改名设计

- 日期：2026-08-06
- 状态：基于 Issue #14、Issue #15、CONTEXT.md 与 ADR-0005/0006 的已授权实施规格
- 基线：`refactor/image-search-pgvector@088bb9f`
- 范围：资产显示名称、乐观并发改名、活动记录基础、双字段普通搜索及相关表示与界面

## 1. 目标与不变量

图片资产新增可编辑、允许重名的 `display_name`。它只用于展示和普通搜索，不参与资产身份，也不改变 `source_relative_path`、OSS 对象键、预览、向量或 embedding。

必须保持以下不变量：

- `source_relative_path` 不可编辑，继续作为来源证明。
- 存量和新建资产的默认显示名称均为来源相对路径最后一段，原样保留扩展名。
- 用户只编辑去除首尾空白后的名称主体，长度为 1 至 100 个 Unicode 字符；扩展名由服务端从不可变来源路径推导。
- 活跃的已归款与未归款资产均可改名；归档资产只读。
- 不同资产可以重名。
- 改名必须携带读取版本；陈旧版本不得覆盖较新修改。
- 成功改名和活动记录在同一 PostgreSQL 事务中提交。
- 普通搜索同时匹配显示名称和来源相对路径，并保留活跃状态、归款筛选、排序和分页。
- 不执行真实数据库迁移、OSS/Kodo 写入、部署、删除或云端操作。

## 2. 方案比较

### 2.1 并发更新

1. **单条条件 UPDATE（采用）**：`WHERE id = ? AND status = 'active' AND version = ? RETURNING ...`。数据库原子判定版本并自增；零行时再读取当前表示以区分不存在、归档和版本冲突。
2. `SELECT FOR UPDATE` 后比较版本：语义直观，但把乐观并发变成串行锁等待，且请求持锁时间更长。
3. SQLAlchemy `version_id_col`：能覆盖 ORM flush，但 bulk SQL、数据库 FK 动作和未来非 ORM 写路径可能绕过；客户端版本比较与冲突响应仍需额外逻辑。

选择方案 1，避免检查与普通 ORM 更新之间的竞争窗口，也不引入两套版本自增机制。

### 2.2 已归款资产界面入口

1. **扩展现有图片资产工作台（采用）**：增加“待归款 / 已归款 / 全部”筛选，复用同一资产卡片和改名编辑器。
2. 只在产品编辑表单内改名：图片目前由 Ant Design Upload 列表承载，改名交互会与产品保存、移除图片语义耦合。
3. 只在以图搜款结果中改名：已归款资产可偶然到达，但图库管理员无法稳定定位目标资产。

选择方案 1，使两类活跃资产都能从产品管理顶层流程到达，同时保持产品表单职责不变。

### 2.3 存量迁移与回滚兼容

1. **显式幂等迁移 + INSERT 触发器（采用）**：迁移回填并收紧非空约束；触发器在旧代码未传 `display_name` 时从来源路径补默认值，使旧版本应用可前向回滚而不破坏新增审计数据。
2. 永久保留可空列并在读取时 `COALESCE`：旧代码兼容简单，但数据库无法保证每项资产具有持久显示名称。
3. 破坏性 down migration：会丢失用户改名和活动记录，不接受。

选择方案 1。回滚只回滚应用代码，保留新增列、触发器和活动记录表；不删除数据结构。

## 3. 数据模型与迁移

### 3.1 `image_assets`

新增：

- `display_name TEXT NOT NULL`
- `version BIGINT NOT NULL DEFAULT 1`
- `CHECK (version >= 1)`

不为 `display_name` 添加唯一约束。ORM 新建资产时显式使用共享的 basename 函数填充名称；数据库触发器是旧代码兼容兜底，不替代应用语义。

### 3.2 `asset_activity_records`

新增可复用活动记录表：

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | UUID | 主键，由应用生成 |
| `event_type` | VARCHAR(64) | 非空 |
| `target_type` | VARCHAR(32) | 非空 |
| `target_id` | TEXT | 非空 |
| `request_id` | VARCHAR(64) | 非空 |
| `source` | VARCHAR(32) | 非空 |
| `actor_id` | TEXT | 可空 |
| `batch_id` | TEXT | 可空 |
| `task_id` | TEXT | 可空 |
| `before_state` | JSONB | 可空 |
| `after_state` | JSONB | 可空 |
| `result` | VARCHAR(32) | 非空 |
| `error_code` | VARCHAR(64) | 可空 |
| `created_at` | TIMESTAMP | 非空，默认 `NOW()` |

索引为 `(target_type, target_id, created_at)` 与 `request_id`。不建立到 `image_assets` 的外键，保证未来资产永久清除后审计记录仍存在。状态摘要只保存显示名称、版本、状态、型号等业务元数据；禁止保存凭证、签名 URL、对象内容和向量。

### 3.3 显式幂等迁移

新增独立迁移模块，由操作员显式传入应用参数后才执行；应用启动、健康检查和普通请求不会引用它。显式 `init_db.py` 建库命令只复用无副作用的触发器 SQL 常量，以保证其创建的新库同样支持应用回滚，不会自动调用整项迁移。迁移步骤：

1. 对 `image_assets` 使用 `ADD COLUMN IF NOT EXISTS` 增加两列。
2. 仅回填 `display_name IS NULL` 的行，basename 保留 Unicode、空格、大小写和原扩展名。
3. 仅回填 `version IS NULL` 的行为 `1`。
4. 安装兼容旧代码的 `BEFORE INSERT` 触发器：仅当 `NEW.display_name IS NULL` 时计算 basename。
5. 校验不存在空 basename，再设置非空、默认值和版本检查约束。
6. 幂等创建活动记录表与索引。
7. 第二次执行不得覆盖已经改过的名称或版本。

新库 `postgres/init/01_init.sql`、SQLAlchemy 模型和迁移后的 schema 保持一致。迁移测试只在 `image_search_test` 的随机临时 schema 中执行。

## 4. 后端接口与事务

### 4.1 名称规范化

共享领域函数负责：

- 从 `/` 分隔的 `source_relative_path` 取最后一段。
- 以最后一个扩展名为不可变扩展名，并保留原大小写。
- 对请求主体执行 `strip()`。
- 拒绝空值、超过 100 字符、`/`、`\\`、Unicode 控制字符、`.` 和 `..`。
- 允许中文、其他 Unicode、内部空格、常见标点和重名。

客户端校验只用于即时反馈，服务端为最终约束。

### 4.2 改名命令

接口：`POST /api/image-assets/<asset_id>/rename`

请求：

```json
{
  "name_body": "夏季新款蓝色挂绳",
  "expected_version": 3
}
```

使用 POST 命令路由以保持现有 CORS 方法集合不变。服务端流程：

1. 校验 JSON、`name_body` 和正整数 `expected_version`。
2. 读取资产的不可变来源路径，用服务端扩展名组装完整名称。
3. 执行单条条件 `UPDATE`，同时设置 `version = version + 1` 与 `updated_at = NOW()`，并 `RETURNING` 最新安全字段。
4. 成功时插入 `asset.rename` 活动记录，名称更新与活动记录同事务提交。
5. 零行时重新读取当前资产：
   - 不存在：404 `IMAGE_ASSET_NOT_FOUND`；
   - 已归档：409 `IMAGE_ASSET_NOT_ACTIVE`，返回 `latest`；
   - 版本不一致：409 `IMAGE_ASSET_VERSION_CONFLICT`，返回 `latest`。
6. 活动记录插入或提交失败时回滚名称更新。

成功响应为 `{"asset": <管理资产安全表示>}`。冲突响应保留稳定 `error`、`error_code` 和 `latest`。

改名路径不构造对象存储或 embedding 适配器，不更新来源路径、OSS key、预览、向量、embedding 模型或归款关系。

## 5. 搜索与表示

`GET /api/image-assets` 保留现有 `assignment`、分页、排序和 `status = 'active'` 条件。`search` 作为普通文本，转义 `%`、`_` 与转义字符后执行：

```sql
display_name ILIKE :pattern ESCAPE '\\'
OR source_relative_path ILIKE :pattern ESCAPE '\\'
```

以下表示均增加 `display_name`、`source_relative_path`、`version`：

- 图片资产管理列表；
- pgvector 图片搜索结果；
- 产品列表中的图片表示；
- `ImageAsset.to_dict()`。

向量搜索保留 `relative_path` 作为短期兼容别名，新增统一的 `source_relative_path`，不改变 active 过滤、Top-K、HNSW 参数、距离或相似度计算。

## 6. 前端交互

产品管理的资产工作台增加归款筛选，并把卡片主次信息固定为：

- 主信息：资产显示名称；
- 次信息：不可变来源相对路径；
- 已归款资产额外显示型号。

共享 `AssetDisplayNameEditor` 负责卡片改名：

- 非编辑态始终显示编辑按钮，不依赖 hover。
- 编辑态只输入名称主体，扩展名只读展示。
- 保存按钮或 Enter 提交；取消按钮或 Esc 放弃并恢复服务器值。
- blur 不保存、不退出编辑，也不丢弃草稿。
- 一般失败保留草稿并显示服务端原因。
- 版本冲突显示服务器最新名称，保留用户草稿，并把下一次显式保存的 expected version 更新为 `latest.version`。
- 成功后仅更新对应卡片表示；若当前搜索条件可能受名称变化影响，再由父级刷新当前页。

以图搜款结果改为显示名称为主、来源路径为次，并使用新的统一 TypeScript 字段；该结果页不承担管理员改名入口。

## 7. 测试与安全边界

### 7.1 后端 PostgreSQL 场景（仅编写，未授权执行）

- 从旧 schema 执行迁移两次，验证多层路径、中文、空格、多点文件名、回填、非覆盖与 schema。
- 新入库资产自动得到 basename 名称和 `version = 1`。
- 已归款/未归款成功，归档拒绝，重复名称允许。
- 1/100 字边界与所有拒绝字符；扩展名内容和大小写不变。
- 两个独立连接使用同一版本并发改名，恰好一个成功，另一个冲突且得到最新表示。
- 活动记录与资产更新原子；伪造活动写失败时名称不变。
- 双字段搜索与 active、assignment、分页组合。
- 向量排序/相似度保持，只增加字段；产品图片表示同步字段。
- 注入会抛错的 OSS/embedding 伪适配器，确认改名不触发外部链路。

上述 PostgreSQL 场景是最终验收所需证据，但当前安全边界禁止加载数据库凭证、连接任何 PostgreSQL 或运行迁移/集成测试。实施期间只运行命名纯函数、Mock session 事务分支、静态 schema/路由合同与前端 fake-API 测试；最终报告必须把 PostgreSQL 场景列为未执行、需用户授权。

### 7.2 前端

- 产品管理顶层流程可切换未归款、已归款和全部资产。
- 卡片主次信息、常驻编辑入口、保存/取消、Enter/Esc、blur 行为。
- 一般失败和 409 均保留草稿；409 展示最新值并允许以新版本显式重试。
- 双字段搜索提示与空状态。
- 定向 Vitest、TypeScript 构建通过。

禁止运行真实 OSS/Kodo 脚本、正式数据库迁移、手工 pgvector benchmark、部署或云端操作。

## 8. 回滚与后续约束

应用回滚为前向兼容回滚：旧代码忽略新增列和活动表；数据库触发器保证旧代码新增资产仍具有默认显示名称。不得通过 DROP 列、DROP 活动表或删除活动记录回滚。

后续归档、恢复等生命周期写操作必须在各自 Ticket 中显式增加 `version = version + 1`，并复用活动记录表；本 Ticket 不实施回收站、异步导入或永久清除。
