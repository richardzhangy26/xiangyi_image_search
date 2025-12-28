# 数据库初始化说明

## 使用 SQL 脚本初始化

### 方法 1：命令行执行（推荐）

```bash
# 1. 进入 backend 目录
cd backend

# 2. 使用 MySQL 客户端执行脚本
mysql -u root -p < init_database.sql

# 输入 MySQL 密码后，脚本会自动执行
```

### 方法 2：交互式执行

```bash
# 1. 登录 MySQL
mysql -u root -p

# 2. 在 MySQL 提示符下执行
mysql> source /path/to/backend/init_database.sql

# 或者
mysql> \. /path/to/backend/init_database.sql
```

### 方法 3：分步执行

```bash
# 1. 创建数据库
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS xiangyipackage_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

# 2. 执行表创建脚本
mysql -u root -p xiangyipackage_test < init_database.sql
```

## 使用 Python 脚本初始化

如果你更喜欢使用 Python（基于 SQLAlchemy ORM）：

```bash
cd backend
python init_new_db.py
```

## 验证数据库

执行以下命令验证数据库是否正确创建：

```bash
mysql -u root -p xiangyipackage_test -e "
SHOW TABLES;
SELECT COUNT(*) FROM products;
SELECT COUNT(*) FROM product_images;
"
```

## 查看表结构

```bash
# 查看 products 表结构
mysql -u root -p xiangyipackage_test -e "DESCRIBE products;"

# 查看 product_images 表结构
mysql -u root -p xiangyipackage_test -e "DESCRIBE product_images;"

# 查看所有索引
mysql -u root -p xiangyipackage_test -e "
SHOW INDEX FROM products;
SHOW INDEX FROM product_images;
"
```

## 重置数据库

如果需要重新初始化数据库：

```bash
# 警告：这会删除所有数据！
mysql -u root -p -e "DROP DATABASE IF EXISTS xiangyipackage_test;"
mysql -u root -p < init_database.sql
```

## 备份数据库

```bash
# 备份整个数据库
mysqldump -u root -p xiangyipackage_test > backup_$(date +%Y%m%d).sql

# 仅备份表结构（不含数据）
mysqldump -u root -p --no-data xiangyipackage_test > schema_only.sql

# 仅备份数据（不含结构）
mysqldump -u root -p --no-create-info xiangyipackage_test > data_only.sql
```

## 恢复数据库

```bash
# 从备份恢复
mysql -u root -p xiangyipackage_test < backup_20250106.sql
```

## Docker 环境中初始化

如果使用 Docker：

```bash
# 方法 1：复制 SQL 文件到容器并执行
docker cp init_database.sql fashion-crm-db:/tmp/
docker exec -i fashion-crm-db mysql -uroot -p${DB_PASSWORD} < /tmp/init_database.sql

# 方法 2：直接通过管道执行
docker exec -i fashion-crm-db mysql -uroot -p${DB_PASSWORD} < init_database.sql
```

## 测试数据

脚本已包含 2 条测试产品数据：
- CS-001: 相机肩带
- HL-002: 相机挂绳

查看测试数据：
```bash
mysql -u root -p xiangyipackage_test -e "SELECT * FROM products;"
```

删除测试数据：
```bash
mysql -u root -p xiangyipackage_test -e "DELETE FROM products WHERE model_number IN ('CS-001', 'HL-002');"
```

## 常见问题

### 1. 权限错误

```bash
# 授予权限
mysql -u root -p -e "GRANT ALL PRIVILEGES ON xiangyipackage_test.* TO 'your_user'@'localhost';"
mysql -u root -p -e "FLUSH PRIVILEGES;"
```

### 2. 字符集问题

确保数据库和表都使用 utf8mb4：
```sql
ALTER DATABASE xiangyipackage_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE products CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

### 3. 外键约束错误

如果遇到外键问题，临时禁用检查：
```sql
SET FOREIGN_KEY_CHECKS=0;
-- 执行操作
SET FOREIGN_KEY_CHECKS=1;
```

## 环境变量配置

确保 `backend/.env` 文件配置正确：

```env
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=your_password
DB_NAME=xiangyipackage_test
```
