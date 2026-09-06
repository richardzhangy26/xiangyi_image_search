# Issue #12：收缩旧产品图片与本地存储路径设计

## 背景与前置证据

Issue #9 已将商品图片写入切换为 `ImageAssetIngestService`，Issue #11 已完成全量迁移与对账。其记录表明生产库有 2,419 条活跃 `image_assets`，而 `product_images` 为空。图片查询已从 `image_assets` 读取，正式图片存储为私有 OSS，站内预览通过短时签名 URL 的 302 提供。

本期是 expand-contract 的 contract 阶段：删除仍可误用的旧运行入口与错误文档假设，但不删除旧数据或云端对象。

## 目标与边界

### 目标

- 活动应用不再创建、服务或持久化本地 `uploads/product_images`。
- 新库初始化不再创建、索引或调整旧 `product_images` 表。
- 旧表若仍存在或未来含有行，只作为只读兼容遗留物；任何发现均生成明确的兼容迁移清单，不触发物理收缩。
- 用户与运维文档只保留一条权威流程：Kodo 只读备份 → 私有 OSS → `image_assets` → 私有预览 302/图片搜索。
- 已退役的七牛公开 URL 和旧目录导入路径不能被误作为正式入口。

### 非目标

- DROP `product_images`、删除本地 `backend/uploads`、Docker volume、Kodo 或 OSS 对象。
- 迁移或补录旧表的历史数据。
- 实现文件夹导入、Excel 导入、永久 purge 或共享对象垃圾回收。

## 方案

采用最小安全收缩，而不是直接删除数据库对象。

1. **运行时收缩**：移除 Flask `/uploads/<path>` 静态服务和启动时的 `uploads/product_images` 建目录逻辑；Docker Compose 移除 `backend/uploads` 的持久化 bind mount，保留运行时临时目录仅用于请求处理。
2. **持久化收缩**：从当前模型、`init_db.py` 与首次启动 SQL 中移除 `ProductImage` 的活动 schema/索引创建；保留一个独立的兼容审计函数，用只读查询检查旧表是否存在及行数。
3. **保护机制**：审计函数对不存在的表、空表、非空表分别返回结构化结果；非空表返回兼容迁移清单（表名、行数、所需人工迁移），不执行 DDL/DML。应用启动不自动运行该审计，更不会删除任何数据。
4. **入口与文档收束**：移除未注册的公开 URL 旧蓝图及公开 URL 拼接工具；旧 `ingest_images.py` 和 `migrate_oss_path.py` 保持不可执行的明确弃用提示，迁移说明只链接 Kodo→OSS 正式流程。

## 数据流与错误处理

```text
上传 / Kodo 迁移
  → ImageAssetIngestService
  → 私有 OSS 原图与 preview-v1
  → image_assets（向量）
  → /api/image-assets/<id>/preview（短时签名 302）
  → 图片级搜索

遗留 product_images
  → 仅显式兼容审计
  → 空 / 不存在：记录状态
  → 非空：返回迁移清单，停止物理收缩
```

所有正常应用路径均不读写旧表，也不假定本地 `uploads` 是永久来源。临时查询文件在请求结束后清理；任何 OSS、embedding 或数据库失败仍沿用资产入库服务的回滚与临时文件清理行为。

## 测试与验收

经确认的测试 seams：

- **部署/启动配置**：组合配置、启动 SQL 和初始化入口不再创建旧表、旧索引或 uploads 持久化路径，且没有隐式 DROP/DELETE。
- **兼容审计接口**：在旧表不存在、为空、非空时产生确定性结果；非空只列出兼容要求，不修改数据。
- **私有预览与写路径**：产品图片和迁移图片仍只由 `ImageAssetIngestService` 写入；预览继续经 `/api/image-assets/<asset_id>/preview` 302。
- **整体回归**：引用审计、后端单元与 PostgreSQL 集成测试、前端类型检查/生产构建，以及真实查询回归。

实现时每个 seam 按 red → green 的纵向切片推进。最终以代码引用、数据库行数、应用日志和 API/前端回归共同验证 contract 前置条件。

## 风险与退出条件

- 因 `product_images` 已知为空，本期可移除活动代码；但不把该事实变成部署时 DROP 的前提。
- 任何未来环境审计若发现旧表非空，必须停止物理收缩，并由后续独立 Issue 制定迁移清单和获得明确授权。
- 删除 `ProductImage` ORM 不代表删除现有生产表；物理数据库收缩需要单独清单、备份和用户明确授权。
