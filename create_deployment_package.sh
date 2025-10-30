#!/bin/bash
# 创建完整部署包
# 功能：将镜像、数据库、配置文件打包成一个压缩文件，方便迁移

set -e

echo "========================================="
echo "创建完整部署包"
echo "========================================="

# 检查依赖
EXPORT_DIR="./docker_export"

echo ""
echo "步骤 1/6: 检查导出文件..."
echo "----------------------------------------"

# 检查必需文件
REQUIRED_FILES=(
    "$EXPORT_DIR/backend.tar"
    "$EXPORT_DIR/frontend.tar"
    "$EXPORT_DIR/database_backup.sql"
)

for file in "${REQUIRED_FILES[@]}"; do
    if [ ! -f "$file" ]; then
        echo "✗ 缺少文件: $file"
        echo ""
        echo "请先运行以下命令:"
        echo "  1. ./export_images.sh    # 导出 Docker 镜像"
        echo "  2. ./export_database.sh  # 导出数据库"
        exit 1
    fi
    echo "✓ 找到文件: $file"
done

echo ""
echo "步骤 2/6: 准备部署文件..."
echo "----------------------------------------"

# 创建临时部署目录
DEPLOY_DIR="$EXPORT_DIR/deploy_package"
mkdir -p "$DEPLOY_DIR"

# 复制必需的配置文件
echo "复制配置文件..."
cp docker-compose.yml "$DEPLOY_DIR/"
cp .env.example "$DEPLOY_DIR/.env.template"

# 如果存在 .env 文件,创建一个脱敏版本
if [ -f .env ]; then
    # 复制 .env 但是隐藏敏感信息
    sed 's/=.*/=YOUR_VALUE_HERE/g' .env > "$DEPLOY_DIR/.env.template.filled"
    echo "✓ 已创建环境变量模板"
fi

echo ""
echo "步骤 3/6: 移动导出文件..."
echo "----------------------------------------"

# 移动镜像文件
mv "$EXPORT_DIR/backend.tar" "$DEPLOY_DIR/"
mv "$EXPORT_DIR/frontend.tar" "$DEPLOY_DIR/"
echo "✓ 镜像文件已移动"

# 移动数据库文件
mv "$EXPORT_DIR/database_backup.sql" "$DEPLOY_DIR/"
cp -r "$EXPORT_DIR/mysql" "$DEPLOY_DIR/" 2>/dev/null || true
echo "✓ 数据库文件已移动"

echo ""
echo "步骤 4/6: 创建导入脚本..."
echo "----------------------------------------"

# 创建导入脚本
cat > "$DEPLOY_DIR/import_and_deploy.sh" << 'IMPORT_SCRIPT'
#!/bin/bash
# 新电脑导入和部署脚本
# 功能：在新电脑上导入镜像、导入数据、启动服务

set -e

echo "========================================="
echo "Fashion CRM 系统部署工具"
echo "========================================="

echo ""
echo "步骤 1/6: 检查环境..."
echo "----------------------------------------"

# 检查 Docker
if ! command -v docker &> /dev/null; then
    echo "✗ 未安装 Docker"
    echo "请先安装 Docker Desktop: https://www.docker.com/products/docker-desktop"
    exit 1
fi
echo "✓ Docker 已安装"

# 检查 Docker Compose
if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo "✗ 未安装 Docker Compose"
    exit 1
fi
echo "✓ Docker Compose 已安装"

echo ""
echo "步骤 2/6: 配置环境变量..."
echo "----------------------------------------"

# 检查 .env 文件
if [ ! -f .env ]; then
    if [ -f .env.template ]; then
        echo "请配置 .env 文件:"
        echo "  1. 复制模板: cp .env.template .env"
        echo "  2. 编辑 .env 文件，填入正确的配置值"
        echo ""
        read -p "是否现在创建 .env 文件? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            cp .env.template .env
            echo "已创建 .env 文件，请编辑后重新运行此脚本"
            exit 0
        else
            exit 1
        fi
    else
        echo "✗ 找不到 .env.template 文件"
        exit 1
    fi
fi

# 加载环境变量
export $(cat .env | grep -v '^#' | xargs)
echo "✓ 环境变量已加载"

echo ""
echo "步骤 3/6: 导入 Docker 镜像..."
echo "----------------------------------------"

# 导入后端镜像
if [ -f backend.tar ]; then
    echo "正在导入后端镜像..."
    docker load -i backend.tar
    echo "✓ 后端镜像导入完成"
else
    echo "✗ 找不到 backend.tar"
    exit 1
fi

# 导入前端镜像
if [ -f frontend.tar ]; then
    echo "正在导入前端镜像..."
    docker load -i frontend.tar
    echo "✓ 前端镜像导入完成"
else
    echo "✗ 找不到 frontend.tar"
    exit 1
fi

echo ""
echo "步骤 4/6: 拉取 MySQL 官方镜像..."
echo "----------------------------------------"
docker pull mysql:8.0
echo "✓ MySQL 镜像拉取完成"

echo ""
echo "步骤 5/6: 启动 MySQL 容器..."
echo "----------------------------------------"

# 启动 MySQL
docker-compose up -d db

echo "等待 MySQL 启动..."
sleep 20

# 检查 MySQL 是否健康
MAX_RETRIES=30
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if docker-compose exec -T db mysqladmin ping -h localhost -u root -p"$DB_PASSWORD" &> /dev/null; then
        echo "✓ MySQL 已就绪"
        break
    fi
    RETRY_COUNT=$((RETRY_COUNT + 1))
    echo "等待 MySQL 启动... ($RETRY_COUNT/$MAX_RETRIES)"
    sleep 2
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo "✗ MySQL 启动超时"
    exit 1
fi

echo ""
echo "步骤 6/6: 导入数据库..."
echo "----------------------------------------"

# 导入数据库备份
if [ -f database_backup.sql ]; then
    echo "正在导入数据库备份..."
    docker-compose exec -T db mysql -u root -p"$DB_PASSWORD" "$DB_NAME" < database_backup.sql
    echo "✓ 数据库导入完成"
else
    echo "警告: 找不到 database_backup.sql，将使用初始化脚本"
fi

echo ""
echo "步骤 7/7: 启动所有服务..."
echo "----------------------------------------"

# 启动所有服务
docker-compose up -d

echo ""
echo "等待服务启动..."
sleep 10

echo ""
echo "========================================="
echo "部署完成!"
echo "========================================="
echo ""
echo "服务访问地址:"
echo "  前端: http://localhost"
echo "  后端: http://localhost:5000"
echo "  数据库: localhost:3307"
echo ""
echo "查看服务状态:"
echo "  docker-compose ps"
echo ""
echo "查看日志:"
echo "  docker-compose logs -f"
echo ""
echo "停止服务:"
echo "  docker-compose down"
echo ""
echo "========================================="
IMPORT_SCRIPT

chmod +x "$DEPLOY_DIR/import_and_deploy.sh"
echo "✓ 导入脚本已创建"

echo ""
echo "步骤 5/6: 创建 README 文件..."
echo "----------------------------------------"

# 创建 README
cat > "$DEPLOY_DIR/README.md" << 'README'
# Fashion CRM 系统部署包

这是一个完整的部署包，包含所有必要的文件用于在新电脑上部署系统。

## 包含内容

- `backend.tar` - 后端 Docker 镜像
- `frontend.tar` - 前端 Docker 镜像
- `database_backup.sql` - 数据库完整备份
- `mysql/` - 数据库初始化脚本
- `docker-compose.yml` - Docker Compose 配置
- `.env.template` - 环境变量模板
- `import_and_deploy.sh` - 一键部署脚本

## 系统要求

- Docker Desktop (或 Docker Engine + Docker Compose)
- 至少 4GB 可用内存
- 至少 10GB 可用磁盘空间

## 快速部署步骤

### 1. 安装 Docker

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
```

#### Windows
```
下载并安装 Docker Desktop
https://www.docker.com/products/docker-desktop
```

### 2. 配置环境变量

```bash
# 复制环境变量模板
cp .env.template .env

# 编辑 .env 文件，填入正确的配置
nano .env  # 或使用其他编辑器
```

必需的环境变量:
- `DB_PASSWORD` - 数据库密码
- `DB_NAME` - 数据库名称
- `DASHSCOPE_API_KEY` - 阿里云 DashScope API 密钥
- `OSS_ACCESS_KEY_ID` - 阿里云 OSS 访问密钥 ID
- `OSS_ACCESS_KEY_SECRET` - 阿里云 OSS 访问密钥
- `OSS_ENDPOINT` - OSS 端点
- `OSS_BUCKET_NAME` - OSS 存储桶名称

### 3. 运行部署脚本

```bash
# 添加执行权限
chmod +x import_and_deploy.sh

# 运行部署
./import_and_deploy.sh
```

### 4. 访问系统

部署完成后，可以通过以下地址访问:

- **前端界面**: http://localhost
- **后端 API**: http://localhost:5000
- **数据库**: localhost:3307

## 手动部署步骤

如果自动部署脚本失败，可以手动执行以下步骤:

### 1. 导入 Docker 镜像

```bash
# 导入后端镜像
docker load -i backend.tar

# 导入前端镜像
docker load -i frontend.tar

# 拉取 MySQL 镜像
docker pull mysql:8.0
```

### 2. 启动 MySQL

```bash
# 仅启动数据库
docker-compose up -d db

# 等待 MySQL 启动 (约 20-30 秒)
sleep 30
```

### 3. 导入数据库

```bash
# 加载环境变量
export $(cat .env | grep -v '^#' | xargs)

# 导入数据库备份
docker-compose exec -T db mysql -u root -p"$DB_PASSWORD" "$DB_NAME" < database_backup.sql
```

### 4. 启动所有服务

```bash
docker-compose up -d
```

## 常用命令

### 查看服务状态
```bash
docker-compose ps
```

### 查看日志
```bash
# 查看所有服务日志
docker-compose logs -f

# 查看特定服务日志
docker-compose logs -f backend
docker-compose logs -f frontend
docker-compose logs -f db
```

### 重启服务
```bash
# 重启所有服务
docker-compose restart

# 重启特定服务
docker-compose restart backend
```

### 停止服务
```bash
# 停止但保留数据
docker-compose stop

# 停止并删除容器 (保留数据卷)
docker-compose down

# 完全清理 (删除所有数据)
docker-compose down -v
```

### 进入容器
```bash
# 进入后端容器
docker-compose exec backend bash

# 进入数据库容器
docker-compose exec db bash

# 进入前端容器
docker-compose exec frontend sh
```

### 数据库操作
```bash
# 连接到 MySQL
docker-compose exec db mysql -u root -p"$DB_PASSWORD" "$DB_NAME"

# 备份数据库
docker-compose exec db mysqldump -u root -p"$DB_PASSWORD" "$DB_NAME" > backup_$(date +%Y%m%d).sql

# 恢复数据库
docker-compose exec -T db mysql -u root -p"$DB_PASSWORD" "$DB_NAME" < backup.sql
```

## 故障排除

### 端口冲突

如果端口已被占用，编辑 `docker-compose.yml` 修改端口映射:

```yaml
services:
  frontend:
    ports:
      - "8080:80"  # 将 80 改为 8080
  backend:
    ports:
      - "5001:5000"  # 将 5000 改为 5001
  db:
    ports:
      - "3308:3306"  # 将 3307 改为 3308
```

### 容器无法启动

```bash
# 查看容器日志
docker-compose logs

# 重新构建并启动
docker-compose down
docker-compose up -d --force-recreate
```

### 数据库连接失败

```bash
# 检查数据库是否健康
docker-compose exec db mysqladmin ping -h localhost -u root -p"$DB_PASSWORD"

# 查看数据库日志
docker-compose logs db

# 重启数据库
docker-compose restart db
```

### 镜像加载失败

```bash
# 检查 Docker 磁盘空间
docker system df

# 清理未使用的资源
docker system prune

# 重新加载镜像
docker load -i backend.tar
docker load -i frontend.tar
```

## 更新和维护

### 更新代码

如果需要更新代码:

1. 在原电脑上重新构建镜像
2. 重新导出镜像
3. 在新电脑上导入新镜像
4. 重启服务

### 备份数据

定期备份数据库:

```bash
# 创建备份
docker-compose exec db mysqldump -u root -p"$DB_PASSWORD" "$DB_NAME" > backup_$(date +%Y%m%d_%H%M%S).sql

# 压缩备份
gzip backup_$(date +%Y%m%d_%H%M%S).sql
```

### 数据卷位置

Docker 数据卷存储位置:
- macOS: `~/Library/Containers/com.docker.docker/Data`
- Linux: `/var/lib/docker/volumes/`
- Windows: `C:\ProgramData\Docker\volumes\`

## 技术支持

如有问题，请检查:
1. Docker 和 Docker Compose 版本
2. 系统资源 (内存、磁盘)
3. 网络连接
4. 环境变量配置
5. 日志输出

## 版本信息

- Docker 镜像: fashion-crm-backend:latest, fashion-crm-frontend:latest
- MySQL: 8.0
- 部署包创建时间: 查看 deployment_info.txt
README

echo "✓ README 已创建"

echo ""
echo "步骤 6/6: 生成部署包信息..."
echo "----------------------------------------"

# 创建部署包信息文件
cat > "$DEPLOY_DIR/deployment_info.txt" << EOF
部署包信息
================
创建时间: $(date '+%Y-%m-%d %H:%M:%S')
系统名称: Fashion CRM 系统

包含文件:
----------------------------------------
1. backend.tar              - 后端 Docker 镜像
2. frontend.tar             - 前端 Docker 镜像
3. database_backup.sql      - 数据库完整备份
4. mysql/                   - 数据库初始化脚本
5. docker-compose.yml       - Docker Compose 配置
6. .env.template            - 环境变量模板
7. import_and_deploy.sh     - 一键部署脚本
8. README.md                - 部署说明文档

文件大小:
----------------------------------------
EOF

cd "$DEPLOY_DIR"
ls -lh | grep -v "^d" | tail -n +2 | awk '{print $9 ": " $5}' >> deployment_info.txt
du -sh mysql 2>/dev/null | awk '{print "mysql/: " $1}' >> deployment_info.txt || true
cd - > /dev/null

echo ""
echo "========================================="
echo "准备压缩部署包..."
echo "========================================="

# 生成压缩包名称
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
PACKAGE_NAME="fashion-crm-deploy-${TIMESTAMP}.tar.gz"

echo ""
echo "正在压缩文件..."
echo "这可能需要几分钟，请耐心等待..."

# 压缩部署包
cd "$EXPORT_DIR"
tar -czf "../$PACKAGE_NAME" deploy_package/
cd - > /dev/null

# 计算压缩包大小和 MD5
PACKAGE_SIZE=$(ls -lh "$PACKAGE_NAME" | awk '{print $5}')
PACKAGE_MD5=$(md5sum "$PACKAGE_NAME" 2>/dev/null | awk '{print $1}' || md5 -q "$PACKAGE_NAME" 2>/dev/null)

echo ""
echo "========================================="
echo "部署包创建完成!"
echo "========================================="
echo ""
echo "部署包信息:"
echo "  文件名: $PACKAGE_NAME"
echo "  大小: $PACKAGE_SIZE"
echo "  MD5: $PACKAGE_MD5"
echo ""
echo "部署包位置:"
echo "  $(pwd)/$PACKAGE_NAME"
echo ""
echo "========================================="
echo "下一步操作:"
echo "========================================="
echo ""
echo "1. 将部署包传输到新电脑:"
echo "   - 使用 U盘/移动硬盘"
echo "   - 或使用 scp/rsync 传输"
echo ""
echo "2. 在新电脑上解压并部署:"
echo "   tar -xzf $PACKAGE_NAME"
echo "   cd deploy_package"
echo "   ./import_and_deploy.sh"
echo ""
echo "详细说明请查看解压后的 README.md 文件"
echo "========================================="

# 清理临时文件
echo ""
read -p "是否清理临时文件? (保留压缩包) (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -rf "$EXPORT_DIR"
    echo "✓ 临时文件已清理"
fi
