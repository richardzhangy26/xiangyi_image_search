# OSS 路径迁移脚本

## 功能说明

此脚本用于将 `product_images` 表中的 `original_path` 字段转换为 OSS 路径，并填入 `oss_path` 字段。

### 转换逻辑

原始路径格式：
```
/Users/richardzhang/github/xiangyipackage/image-search-engine/backend/data/摄像师拍摄素材/2025.4.18海报照片/加拍照片/DSC01390.jpg
```

转换后的 OSS 路径：
```
http://t4u5e1e4j.hd-bkt.clouddn.com/2025.4.18海报照片/加拍照片/DSC01390.jpg
```

脚本会自动提取 `摄像师拍摄素材/` 之后的相对路径部分，并拼接到 OSS 基础 URL。

## 使用方法

### 方式一：使用运行脚本（推荐）

```bash
cd backend
./run_oss_migration.sh
```

这个脚本会：
1. 首先预览前 5 条记录的转换结果
2. 询问是否继续处理所有记录
3. 如果确认，则处理所有记录

### 方式二：直接运行 Python 脚本

```bash
cd backend

# 1. 预览模式（不修改数据库）- 查看前 5 条记录
export DB_HOST=localhost
export DB_USER=root
export DB_PASSWORD=your_password
export DB_NAME=xiangyipackage

python scripts/migrate_oss_path.py --dry-run --limit 5

# 2. 实际执行（仅更新 oss_path 为空的记录）
python scripts/migrate_oss_path.py

# 3. 强制更新所有记录（包括已有 oss_path 的记录）
python scripts/migrate_oss_path.py --force

# 4. 只处理前 100 条记录
python scripts/migrate_oss_path.py --limit 100
```

## 命令行参数

- `--dry-run`: 预览模式，不实际修改数据库
- `--limit N`: 仅处理前 N 条记录
- `--force`: 强制更新所有记录，包括已有 oss_path 的记录
- `--batch-size N`: 批量提交大小，默认 100

## 示例输出

```
[2025-10-29 17:43:06] [INFO] 找到 2418 条需要处理的记录
[2025-10-29 17:43:06] [INFO] 限制处理前 5 条记录
[2025-10-29 17:43:06] [INFO] [1/5] [DRY-RUN] ID=1
[2025-10-29 17:43:06] [INFO]   原始路径: /Users/.../摄像师拍摄素材/2025.4.18海报照片/0882ebe0e2613c5d662332b4bd85f64.png
[2025-10-29 17:43:06] [INFO]   OSS路径:  http://t4u5e1e4j.hd-bkt.clouddn.com/2025.4.18海报照片/0882ebe0e2613c5d662332b4bd85f64.png
...
[2025-10-29 17:43:06] [INFO] ============================================================
[2025-10-29 17:43:06] [INFO] 处理完成:
[2025-10-29 17:43:06] [INFO]   总记录数: 2418
[2025-10-29 17:43:06] [INFO]   处理数量: 5
[2025-10-29 17:43:06] [INFO]   更新成功: 5
[2025-10-29 17:43:06] [INFO]   跳过记录: 0
[2025-10-29 17:43:06] [INFO]   错误记录: 0
```

## 注意事项

1. **备份数据库**：执行前建议先备份数据库
2. **测试先行**：先使用 `--dry-run --limit 5` 预览结果
3. **增量更新**：默认只更新 `oss_path` 为空的记录，已有值的记录不会被覆盖
4. **强制更新**：如需重新生成所有 OSS 路径，使用 `--force` 参数
5. **路径格式**：确保 `original_path` 包含 `摄像师拍摄素材/` 路径段

## 配置修改

如需修改 OSS 基础 URL 或路径提取逻辑，请编辑 `scripts/migrate_oss_path.py` 文件：

```python
# OSS 基础 URL
OSS_BASE_URL = "http://t4u5e1e4j.hd-bkt.clouddn.com"

# 基础路径（用于提取相对路径）
DATASET_BASE_PATH = "/Users/richardzhang/github/xiangyipackage/image-search-engine/backend/data/摄像师拍摄素材"
```

## 故障排查

### 问题：提示找不到 app 模块

确保从 `backend` 目录运行脚本，或设置 `PYTHONPATH`：

```bash
cd backend
python scripts/migrate_oss_path.py
```

### 问题：数据库连接失败

检查环境变量是否正确设置：

```bash
export DB_HOST=localhost
export DB_USER=root
export DB_PASSWORD=your_password
export DB_NAME=xiangyipackage
```

### 问题：路径提取不正确

检查 `original_path` 格式是否符合预期，如有特殊格式，需要修改 `extract_relative_path()` 函数。
