# Docker 镜像导出和部署指南

## 概述

本指南提供了将 Fashion CRM 系统从一台电脑迁移到另一台电脑的完整流程,包括 Docker 镜像导出、数据库备份、以及在新环境中的部署。

## 目录

- [原电脑操作](#原电脑操作)
- [传输文件](#传输文件)
- [新电脑操作](#新电脑操作)
- [验证部署](#验证部署)
- [故障排除](#故障排除)

---

## 原电脑操作

### 前提条件

- Docker 和 Docker Compose 已安装并运行
- 项目容器正在运行或已构建镜像
- 确保有足够的磁盘空间 (建议至少 10GB)

### 一键导出 (推荐)

运行以下三个脚本即可完成所有导出工作:

```bash
# 步骤 1: 导出 Docker 镜像
./export_images.sh

# 步骤 2: 导出数据库
./export_database.sh

# 步骤 3: 创建完整部署包
./create_deployment_package.sh
```

最终会生成一个压缩包,例如: `fashion-crm-deploy-20241030_153000.tar.gz`

### 手动导出 (可选)

如果需要更细粒度的控制,可以手动执行以下步骤:

#### 1. 导出 Docker 镜像

```bash
# 创建导出目录
mkdir -p docker_export

# 导出后端镜像
docker save -o docker_export/backend.tar fashion-crm-backend:latest

# 导出前端镜像
docker save -o docker_export/frontend.tar fashion-crm-frontend:latest
```

#### 2. 导出数据库

```bash
# 方法 1: 从运行中的容器导出
docker exec fashion-crm-db mysqldump \
  -u root \
  -p"YOUR_PASSWORD" \
  --single-transaction \
  --routines \
  --triggers \
  --events \
  --hex-blob \
  YOUR_DB_NAME > docker_export/database_backup.sql

# 方法 2: 如果容器未运行,先启动数据库
docker-compose up -d db
sleep 20  # 等待 MySQL 启动
# 然后执行上面的导出命令
```

#### 3. 复制配置文件

```bash
# 复制必要的配置文件
cp docker-compose.yml docker_export/
cp .env.example docker_export/.env.template
cp -r mysql docker_export/
```

---

## 传输文件

### 方法 1: 使用物理介质 (U盘/移动硬盘)

```bash
# 将部署包复制到 U盘
cp fashion-crm-deploy-*.tar.gz /Volumes/YOUR_USB_DRIVE/

# 或复制整个 docker_export 目录
cp -r docker_export /Volumes/YOUR_USB_DRIVE/
```

### 方法 2: 使用 SCP (网络传输)

```bash
# 传输压缩包到远程服务器
scp fashion-crm-deploy-*.tar.gz user@remote-server:/path/to/destination/

# 或传输整个目录
scp -r docker_export user@remote-server:/path/to/destination/
```

### 方法 3: 使用云存储

```bash
# 上传到云存储 (例如 Aliyun OSS, AWS S3 等)
# 使用相应的 CLI 工具上传

# 阿里云 OSS 示例
ossutil cp fashion-crm-deploy-*.tar.gz oss://your-bucket/

# AWS S3 示例
aws s3 cp fashion-crm-deploy-*.tar.gz s3://your-bucket/
```

---

## 新电脑操作

### 前提条件

确保新电脑已安装 Docker:

#### macOS

```bash
# 下载并安装 Docker Desktop
# https://www.docker.com/products/docker-desktop
```

#### Linux (Ubuntu/Debian)

```bash
# 安装 Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# 安装 Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# 将当前用户添加到 docker 组
sudo usermod -aG docker $USER
newgrp docker
```

#### Windows

```
下载并安装 Docker Desktop
https://www.docker.com/products/docker-desktop
```

### 一键部署 (推荐)

```bash
# 1. 解压部署包
tar -xzf fashion-crm-deploy-*.tar.gz
cd deploy_package

# 2. 配置环境变量
cp .env.template .env
nano .env  # 或使用其他编辑器编辑

# 3. 运行部署脚本
chmod +x import_and_deploy.sh
./import_and_deploy.sh
```

### 手动部署 (可选)

如果自动部署失败,可以手动执行:

#### 1. 导入 Docker 镜像

```bash
# 导入后端镜像
docker load -i backend.tar

# 导入前端镜像
docker load -i frontend.tar

# 拉取 MySQL 官方镜像
docker pull mysql:8.0

# 验证镜像已导入
docker images | grep fashion-crm
```

#### 2. 配置环境变量

创建 `.env` 文件并配置以下变量:

```bash
# 数据库配置
DB_HOST=db
DB_PORT=3306
DB_NAME=your_database_name
DB_PASSWORD=your_secure_password

# API 密钥
DASHSCOPE_API_KEY=your_dashscope_key
DEEPSEEK_API_KEY=your_deepseek_key

# OSS 配置
OSS_ACCESS_KEY_ID=your_oss_key_id
OSS_ACCESS_KEY_SECRET=your_oss_key_secret
OSS_ENDPOINT=your_oss_endpoint
OSS_BUCKET_NAME=your_bucket_name

# 前端配置
VITE_API_BASE_URL=http://localhost:5000
```

#### 3. 启动 MySQL 容器

```bash
# 启动 MySQL 容器
docker-compose up -d db

# 等待 MySQL 启动
echo "等待 MySQL 启动..."
sleep 30

# 验证 MySQL 是否健康
docker-compose exec db mysqladmin ping -h localhost -u root -p"$DB_PASSWORD"
```

#### 4. 导入数据库

```bash
# 加载环境变量
export $(cat .env | grep -v '^#' | xargs)

# 导入数据库备份
docker-compose exec -T db mysql -u root -p"$DB_PASSWORD" "$DB_NAME" < database_backup.sql

# 验证数据导入
docker-compose exec db mysql -u root -p"$DB_PASSWORD" "$DB_NAME" -e "SHOW TABLES;"
```

#### 5. 启动所有服务

```bash
# 启动所有服务
docker-compose up -d

# 查看服务状态
docker-compose ps

# 查看日志
docker-compose logs -f
```

---

## 验证部署

### 1. 检查容器状态

```bash
# 查看所有容器
docker-compose ps

# 应该看到 3 个容器都在运行:
# - fashion-crm-db (健康状态)
# - fashion-crm-backend (健康状态)
# - fashion-crm-frontend (健康状态)
```

### 2. 测试服务访问

```bash
# 测试后端 API
curl http://localhost:5000/api/health

# 测试前端
curl http://localhost/

# 测试数据库连接
docker-compose exec db mysql -u root -p"$DB_PASSWORD" "$DB_NAME" -e "SELECT COUNT(*) FROM products;"
```

### 3. 浏览器访问

打开浏览器访问:

- **前端界面**: http://localhost
- **后端 API**: http://localhost:5000

### 4. 功能测试

1. 登录系统
2. 测试产品搜索功能
3. 测试图片上传功能
4. 测试订单管理功能
5. 测试客户管理功能

---

## 常用命令

### 容器管理

```bash
# 查看容器状态
docker-compose ps

# 查看实时日志
docker-compose logs -f

# 查看特定服务日志
docker-compose logs -f backend
docker-compose logs -f frontend
docker-compose logs -f db

# 重启服务
docker-compose restart

# 重启特定服务
docker-compose restart backend

# 停止服务
docker-compose stop

# 停止并删除容器 (保留数据)
docker-compose down

# 完全清理 (删除所有数据)
docker-compose down -v
```

### 数据库操作

```bash
# 连接到数据库
docker-compose exec db mysql -u root -p"$DB_PASSWORD" "$DB_NAME"

# 备份数据库
docker-compose exec db mysqldump -u root -p"$DB_PASSWORD" "$DB_NAME" > backup_$(date +%Y%m%d).sql

# 恢复数据库
docker-compose exec -T db mysql -u root -p"$DB_PASSWORD" "$DB_NAME" < backup.sql

# 查看数据库大小
docker-compose exec db mysql -u root -p"$DB_PASSWORD" -e "
  SELECT
    table_schema AS 'Database',
    ROUND(SUM(data_length + index_length) / 1024 / 1024, 2) AS 'Size (MB)'
  FROM information_schema.tables
  WHERE table_schema = '$DB_NAME'
  GROUP BY table_schema;
"
```

### 进入容器

```bash
# 进入后端容器
docker-compose exec backend bash

# 进入前端容器
docker-compose exec frontend sh

# 进入数据库容器
docker-compose exec db bash
```

---

## 故障排除

### 问题 1: 端口冲突

**症状**: 容器无法启动,提示端口已被占用

**解决方案**:

```bash
# 查看端口占用
# macOS/Linux
lsof -i :80
lsof -i :5000
lsof -i :3307

# Windows
netstat -ano | findstr :80
netstat -ano | findstr :5000
netstat -ano | findstr :3307

# 修改 docker-compose.yml 中的端口映射
# 例如:
# ports:
#   - "8080:80"  # 前端
#   - "5001:5000"  # 后端
#   - "3308:3306"  # 数据库
```

### 问题 2: 数据库连接失败

**症状**: 后端无法连接到数据库

**解决方案**:

```bash
# 1. 检查数据库是否健康
docker-compose ps db

# 2. 查看数据库日志
docker-compose logs db

# 3. 测试数据库连接
docker-compose exec db mysqladmin ping -h localhost -u root -p"$DB_PASSWORD"

# 4. 检查环境变量
docker-compose exec backend env | grep DB_

# 5. 重启数据库
docker-compose restart db
```

### 问题 3: 镜像加载失败

**症状**: 无法加载 .tar 镜像文件

**解决方案**:

```bash
# 1. 检查 Docker 磁盘空间
docker system df

# 2. 清理未使用的资源
docker system prune -a

# 3. 验证 tar 文件完整性
tar -tzf backend.tar > /dev/null
tar -tzf frontend.tar > /dev/null

# 4. 重新加载镜像
docker load -i backend.tar
docker load -i frontend.tar

# 5. 验证镜像已加载
docker images | grep fashion-crm
```

### 问题 4: 容器健康检查失败

**症状**: 容器状态显示 unhealthy

**解决方案**:

```bash
# 1. 查看健康检查日志
docker inspect --format='{{json .State.Health}}' fashion-crm-backend | jq

# 2. 手动测试健康检查命令
docker-compose exec backend python -c "import requests; print(requests.get('http://localhost:5000/api/health').status_code)"

# 3. 查看容器日志
docker-compose logs backend

# 4. 进入容器检查
docker-compose exec backend bash
curl http://localhost:5000/api/health
```

### 问题 5: 数据导入失败

**症状**: database_backup.sql 导入时出错

**解决方案**:

```bash
# 1. 检查 SQL 文件是否损坏
head -n 10 database_backup.sql
tail -n 10 database_backup.sql

# 2. 分段导入
split -l 10000 database_backup.sql backup_part_

# 3. 使用初始化脚本重建表结构
docker-compose exec -T db mysql -u root -p"$DB_PASSWORD" "$DB_NAME" < mysql/init.sql

# 4. 尝试忽略错误继续导入
docker-compose exec -T db mysql -u root -p"$DB_PASSWORD" --force "$DB_NAME" < database_backup.sql
```

### 问题 6: 环境变量未生效

**症状**: 服务无法读取正确的配置

**解决方案**:

```bash
# 1. 检查 .env 文件格式
cat .env | grep -v '^#' | grep '='

# 2. 验证环境变量
docker-compose config

# 3. 重新创建容器
docker-compose down
docker-compose up -d

# 4. 检查容器内的环境变量
docker-compose exec backend env
```

---

## 性能优化

### 减小镜像大小

```bash
# 使用多阶段构建
# 在 Dockerfile 中已实现

# 清理构建缓存
docker builder prune

# 压缩导出的 tar 文件
gzip backend.tar
gzip frontend.tar
```

### 加速数据库导入

```bash
# 临时禁用约束检查
docker-compose exec db mysql -u root -p"$DB_PASSWORD" "$DB_NAME" << EOF
SET FOREIGN_KEY_CHECKS=0;
SET UNIQUE_CHECKS=0;
SET AUTOCOMMIT=0;
SOURCE database_backup.sql;
COMMIT;
SET FOREIGN_KEY_CHECKS=1;
SET UNIQUE_CHECKS=1;
EOF
```

### 使用数据卷优化

```bash
# 检查数据卷使用情况
docker volume ls
docker volume inspect fashion-crm_mysql_data

# 备份数据卷
docker run --rm -v fashion-crm_mysql_data:/data -v $(pwd):/backup \
  alpine tar czf /backup/mysql_data_backup.tar.gz /data
```

---

## 安全建议

### 1. 保护敏感信息

```bash
# 不要将 .env 文件包含在部署包中
# 使用 .env.template 代替

# 生成强密码
openssl rand -base64 32

# 加密传输文件
gpg -c fashion-crm-deploy.tar.gz
```

### 2. 限制网络访问

```yaml
# 在 docker-compose.yml 中添加网络隔离
networks:
  app-network:
    driver: bridge
    internal: true  # 仅内部通信
```

### 3. 定期备份

```bash
# 创建自动备份脚本
cat > backup.sh << 'EOF'
#!/bin/bash
BACKUP_DIR="/path/to/backups"
DATE=$(date +%Y%m%d_%H%M%S)

# 备份数据库
docker-compose exec -T db mysqldump -u root -p"$DB_PASSWORD" "$DB_NAME" > "$BACKUP_DIR/db_$DATE.sql"

# 备份数据卷
docker run --rm -v fashion-crm_mysql_data:/data -v $BACKUP_DIR:/backup \
  alpine tar czf /backup/volume_$DATE.tar.gz /data

# 保留最近 7 天的备份
find $BACKUP_DIR -name "*.sql" -mtime +7 -delete
find $BACKUP_DIR -name "*.tar.gz" -mtime +7 -delete
EOF

chmod +x backup.sh

# 添加到 crontab (每天凌晨 2 点备份)
(crontab -l 2>/dev/null; echo "0 2 * * * /path/to/backup.sh") | crontab -
```

---

## 附录

### A. 完整文件清单

部署包包含以下文件:

```
deploy_package/
├── backend.tar                 # 后端 Docker 镜像
├── frontend.tar                # 前端 Docker 镜像
├── database_backup.sql         # 数据库完整备份
├── mysql/                      # 数据库初始化脚本
│   ├── init.sql               # 表结构定义
│   └── initial.sql            # 初始数据 (可选)
├── docker-compose.yml          # Docker Compose 配置
├── .env.template               # 环境变量模板
├── import_and_deploy.sh        # 一键部署脚本
├── README.md                   # 部署说明
└── deployment_info.txt         # 部署包信息
```

### B. 环境变量说明

| 变量名 | 说明 | 示例 | 必需 |
|--------|------|------|------|
| DB_HOST | 数据库主机 | db | ✓ |
| DB_PORT | 数据库端口 | 3306 | ✓ |
| DB_NAME | 数据库名称 | fashion_crm | ✓ |
| DB_PASSWORD | 数据库密码 | your_password | ✓ |
| DASHSCOPE_API_KEY | DashScope API 密钥 | sk-xxx | ✓ |
| DEEPSEEK_API_KEY | DeepSeek API 密钥 | sk-xxx | ✗ |
| OSS_ACCESS_KEY_ID | OSS 访问密钥 ID | LTAI5xxx | ✓ |
| OSS_ACCESS_KEY_SECRET | OSS 访问密钥 | your_secret | ✓ |
| OSS_ENDPOINT | OSS 端点 | oss-cn-hangzhou.aliyuncs.com | ✓ |
| OSS_BUCKET_NAME | OSS 存储桶 | your-bucket | ✓ |
| VITE_API_BASE_URL | 前端 API 地址 | http://localhost:5000 | ✗ |

### C. 系统要求

**最低配置**:
- CPU: 2 核
- 内存: 4GB
- 磁盘: 20GB
- Docker: 20.10+
- Docker Compose: 1.29+

**推荐配置**:
- CPU: 4 核
- 内存: 8GB
- 磁盘: 50GB SSD
- Docker: 24.0+
- Docker Compose: 2.0+

### D. 支持的操作系统

- macOS 10.15+
- Ubuntu 20.04+
- Debian 11+
- CentOS 8+
- Windows 10/11 (使用 Docker Desktop)

---

## 更新日志

### 2024-10-30
- ✅ 创建初始部署脚本
- ✅ 添加一键导出功能
- ✅ 添加一键部署功能
- ✅ 完善文档和故障排除

---

## 许可证

本项目为内部使用,请勿外传。

---

## 联系方式

如有问题,请联系技术支持团队。
