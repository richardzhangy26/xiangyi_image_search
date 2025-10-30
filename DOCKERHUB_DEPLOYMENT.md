# Docker Hub 部署指南

使用 Docker Hub Private Registry 进行镜像分发和部署的完整指南。

## 📋 方案概述

相比传输 tar 文件，使用 Docker Hub 有以下优势:

✅ **无需传输大文件** - 不需要 U盘或网络传输几个 GB 的 tar 文件
✅ **版本管理** - 可以保存多个版本的镜像
✅ **随时拉取** - 任何有权限的电脑都可以随时拉取最新镜像
✅ **自动化** - 可以集成 CI/CD 自动推送镜像
✅ **节省空间** - Docker Hub 会压缩和去重镜像层

---

## 🚀 快速开始

### 方案 A: Docker Hub (推荐)

#### 在原电脑上操作

```bash
# 1. 推送镜像到 Docker Hub
./push_to_dockerhub.sh

# 2. 导出数据库
./export_database.sh
```

#### 传输数据库文件

只需传输数据库文件 (几十 MB)，而不是整个镜像 (几个 GB):

```bash
# 方法 1: 使用 SCP
scp docker_export/database_backup.sql user@new-computer:/path/to/project/

# 方法 2: 使用云存储
# 上传到 OSS/S3 等，然后在新电脑下载

# 方法 3: 使用 Git LFS (如果数据库不大)
git lfs track "*.sql"
git add database_backup.sql
git commit -m "Add database backup"
git push
```

#### 在新电脑上操作

```bash
# 1. 克隆项目代码
git clone <your-repo-url>
cd <project-directory>

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 文件

# 3. 下载数据库文件 (如果没有在代码库中)
# scp user@old-computer:/path/to/database_backup.sql ./

# 4. 拉取镜像并部署
./pull_from_dockerhub.sh
```

---

## 📦 详细步骤

### 步骤 1: 准备 Docker Hub 账号

#### 1.1 注册 Docker Hub 账号

访问 https://hub.docker.com/ 注册账号 (如果没有)

#### 1.2 创建私有仓库 (可选)

如果需要保密:
1. 登录 Docker Hub
2. 点击 "Create Repository"
3. 设置为 "Private"
4. 创建两个仓库:
   - `fashion-crm-backend`
   - `fashion-crm-frontend`

> **注意**: Docker Hub 免费账户只能有 1 个私有仓库，如果需要多个私有仓库需要订阅 Pro 计划 ($5/月)

---

### 步骤 2: 原电脑操作

#### 2.1 推送镜像到 Docker Hub

```bash
# 运行推送脚本
./push_to_dockerhub.sh
```

脚本会:
1. ✅ 检查本地镜像是否存在
2. ✅ 登录 Docker Hub
3. ✅ 为镜像打标签
4. ✅ 推送到 Docker Hub
5. ✅ 可选: 创建版本标签

**示例输出:**
```
=========================================
Docker Hub 镜像推送工具
=========================================

步骤 1/5: 检查本地镜像...
✓ 找到后端镜像: fashion-crm-backend:latest
✓ 找到前端镜像: fashion-crm-frontend:latest

步骤 2/5: 登录 Docker Hub...
✓ 已登录 Docker Hub (用户: your-username)

步骤 3/5: 为镜像打标签...
标记后端镜像: fashion-crm-backend:latest -> your-username/fashion-crm-backend:latest
标记前端镜像: fashion-crm-frontend:latest -> your-username/fashion-crm-frontend:latest

步骤 4/5: 推送镜像到 Docker Hub...
正在推送后端镜像...
✓ 后端镜像推送成功
✓ 前端镜像推送成功

步骤 5/5: 生成镜像信息文件...
✓ 镜像信息已保存
```

#### 2.2 导出数据库

```bash
# 运行数据库导出脚本
./export_database.sh
```

这会创建:
- `docker_export/database_backup.sql` - 完整数据库备份
- `docker_export/mysql/` - 初始化脚本

#### 2.3 设置镜像为私有 (可选)

1. 访问 https://hub.docker.com/u/your-username
2. 点击仓库名称
3. 进入 "Settings" 标签
4. 在 "Visibility" 部分选择 "Make private"

---

### 步骤 3: 传输数据库文件

只需传输数据库文件 (比镜像小得多):

#### 方法 1: 网络传输 (推荐)

```bash
# SCP 传输
scp docker_export/database_backup.sql user@new-computer:/path/to/project/

# 或者打包传输
cd docker_export
tar -czf database.tar.gz database_backup.sql mysql/
scp database.tar.gz user@new-computer:/path/to/project/
```

#### 方法 2: 云存储

```bash
# 阿里云 OSS
ossutil cp docker_export/database_backup.sql oss://your-bucket/

# AWS S3
aws s3 cp docker_export/database_backup.sql s3://your-bucket/

# 在新电脑上下载
ossutil cp oss://your-bucket/database_backup.sql ./
```

#### 方法 3: Git Repository (小数据库)

如果数据库备份文件不大 (<100MB):

```bash
# 添加到 .gitignore 排除列表的例外
echo "!database_backup.sql" >> .gitignore

# 提交
git add docker_export/database_backup.sql
git commit -m "Add database backup for deployment"
git push
```

#### 方法 4: U盘/移动硬盘

只需复制 `docker_export` 目录 (几十 MB 而不是几 GB)

---

### 步骤 4: 新电脑操作

#### 4.1 准备环境

确保安装了 Docker 和 Docker Compose:

**macOS:**
```bash
# 安装 Docker Desktop
# https://www.docker.com/products/docker-desktop
```

**Linux:**
```bash
# 安装 Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# 安装 Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose
```

#### 4.2 获取项目代码

```bash
# 克隆代码库
git clone <your-repo-url>
cd <project-directory>

# 或者手动创建项目目录并复制文件
mkdir fashion-crm
cd fashion-crm
# 复制 docker-compose.yml, .env.example, pull_from_dockerhub.sh 等
```

#### 4.3 配置环境变量

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑配置
nano .env  # 或使用其他编辑器
```

必需配置的变量:
```bash
# 数据库配置
DB_PASSWORD=your_secure_password
DB_NAME=fashion_crm

# API 密钥
DASHSCOPE_API_KEY=your_dashscope_key
DEEPSEEK_API_KEY=your_deepseek_key  # 可选

# OSS 配置
OSS_ACCESS_KEY_ID=your_oss_key_id
OSS_ACCESS_KEY_SECRET=your_oss_key_secret
OSS_ENDPOINT=oss-cn-hangzhou.aliyuncs.com
OSS_BUCKET_NAME=your_bucket_name
```

#### 4.4 放置数据库文件

将数据库备份文件放在项目根目录:

```bash
# 如果通过 SCP 传输
# (文件应该已经在正确位置)

# 如果通过云存储下载
ossutil cp oss://your-bucket/database_backup.sql ./

# 如果通过 Git 拉取
git pull

# 如果从 tar 包解压
tar -xzf database.tar.gz

# 确认文件存在
ls -lh database_backup.sql
```

#### 4.5 运行部署脚本

```bash
# 添加执行权限
chmod +x pull_from_dockerhub.sh

# 运行部署
./pull_from_dockerhub.sh
```

脚本会自动:
1. ✅ 检查 Docker 环境
2. ✅ 验证环境变量
3. ✅ 登录 Docker Hub
4. ✅ 拉取镜像
5. ✅ 启动 MySQL
6. ✅ 导入数据库
7. ✅ 启动所有服务

**示例输出:**
```
=========================================
Docker Hub 镜像拉取和部署工具
=========================================

步骤 1/7: 检查环境...
✓ Docker 已安装
✓ Docker Compose 已安装

步骤 2/7: 配置环境变量...
✓ 环境变量已加载

步骤 3/7: 登录 Docker Hub...
✓ Docker Hub 登录成功

步骤 4/7: 拉取 Docker 镜像...
正在拉取后端镜像...
✓ 后端镜像拉取成功
✓ 前端镜像拉取成功
✓ MySQL 镜像拉取成功

步骤 5/7: 启动 MySQL 容器...
✓ MySQL 已就绪

步骤 6/7: 导入数据库...
✓ 数据库导入成功

步骤 7/7: 启动所有服务...
✓ 所有服务已启动

=========================================
部署完成!
=========================================

服务访问地址:
  前端: http://localhost
  后端: http://localhost:5000
  数据库: localhost:3307
```

---

## 🔄 更新镜像

### 在原电脑上更新代码后

```bash
# 1. 重新构建镜像
docker-compose build

# 2. 推送新镜像
./push_to_dockerhub.sh

# 3. (可选) 创建版本标签
# 脚本会提示是否创建版本标签
```

### 在新电脑上更新

```bash
# 1. 拉取最新镜像
docker-compose pull

# 2. 重启服务
docker-compose down
docker-compose up -d

# 或者直接运行
./pull_from_dockerhub.sh
```

---

## 🔐 安全建议

### 1. 使用私有仓库

对于生产环境或敏感项目，建议使用私有仓库:

- Docker Hub Pro ($5/月): 无限私有仓库
- 企业级: 阿里云容器镜像服务、AWS ECR、Google GCR

### 2. 使用访问令牌

而不是密码登录:

```bash
# 1. 在 Docker Hub 创建访问令牌
# Account Settings -> Security -> New Access Token

# 2. 使用令牌登录
docker login -u your-username
# 输入令牌而不是密码
```

### 3. 限制镜像访问

在 Docker Hub 设置中:
- 添加协作者 (Collaborators)
- 设置团队权限

### 4. 加密数据库备份

```bash
# 加密数据库文件
gpg -c database_backup.sql

# 传输加密文件
scp database_backup.sql.gpg user@new-computer:/path/

# 在新电脑解密
gpg -d database_backup.sql.gpg > database_backup.sql
```

---

## 📊 两种方案对比

| 特性 | Docker Hub 方案 | Tar 文件方案 |
|------|-----------------|--------------|
| 传输大小 | 小 (仅数据库,几十 MB) | 大 (镜像+数据库,几 GB) |
| 传输方式 | 网络拉取 + 数据库文件 | 物理介质或网络 |
| 版本管理 | ✅ 支持多版本 | ❌ 需手动管理 |
| 更新便利性 | ✅ 随时拉取最新 | ❌ 需重新导出 |
| 离线部署 | ❌ 需网络连接 | ✅ 完全离线 |
| 存储成本 | ✅ Docker Hub 托管 | ❌ 本地存储 |
| 适用场景 | 多环境部署、频繁更新 | 离线环境、一次性迁移 |

---

## 🛠️ 高级用法

### 使用私有镜像仓库

#### 阿里云容器镜像服务

```bash
# 1. 登录阿里云镜像仓库
docker login --username=your-username registry.cn-hangzhou.aliyuncs.com

# 2. 打标签
docker tag fashion-crm-backend:latest registry.cn-hangzhou.aliyuncs.com/your-namespace/fashion-crm-backend:latest

# 3. 推送
docker push registry.cn-hangzhou.aliyuncs.com/your-namespace/fashion-crm-backend:latest

# 4. 在新电脑拉取
docker pull registry.cn-hangzhou.aliyuncs.com/your-namespace/fashion-crm-backend:latest
```

#### 自建 Docker Registry

```bash
# 1. 启动私有 Registry
docker run -d -p 5000:5000 --name registry registry:2

# 2. 打标签
docker tag fashion-crm-backend:latest localhost:5000/fashion-crm-backend:latest

# 3. 推送
docker push localhost:5000/fashion-crm-backend:latest

# 4. 在新电脑拉取 (需要配置 insecure-registries)
docker pull your-server:5000/fashion-crm-backend:latest
```

### CI/CD 集成

#### GitHub Actions 示例

```yaml
name: Build and Push to Docker Hub

on:
  push:
    branches: [ main ]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2

      - name: Login to Docker Hub
        uses: docker/login-action@v1
        with:
          username: ${{ secrets.DOCKER_USERNAME }}
          password: ${{ secrets.DOCKER_TOKEN }}

      - name: Build and push backend
        uses: docker/build-push-action@v2
        with:
          context: ./backend
          push: true
          tags: ${{ secrets.DOCKER_USERNAME }}/fashion-crm-backend:latest

      - name: Build and push frontend
        uses: docker/build-push-action@v2
        with:
          context: ./frontend
          push: true
          tags: ${{ secrets.DOCKER_USERNAME }}/fashion-crm-frontend:latest
```

### 多环境部署

使用不同的标签管理不同环境:

```bash
# 开发环境
docker tag fashion-crm-backend:latest your-username/fashion-crm-backend:dev
docker push your-username/fashion-crm-backend:dev

# 测试环境
docker tag fashion-crm-backend:latest your-username/fashion-crm-backend:staging
docker push your-username/fashion-crm-backend:staging

# 生产环境
docker tag fashion-crm-backend:latest your-username/fashion-crm-backend:prod
docker push your-username/fashion-crm-backend:prod

# 在 docker-compose.yml 中使用
# image: your-username/fashion-crm-backend:${ENV_TAG:-latest}
```

---

## ❓ 常见问题

### Q1: Docker Hub 拉取速度慢怎么办?

**A:** 使用镜像加速器:

```bash
# macOS Docker Desktop
# Preferences -> Docker Engine -> 添加:
{
  "registry-mirrors": [
    "https://registry.docker-cn.com",
    "https://docker.mirrors.ustc.edu.cn"
  ]
}

# Linux
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json <<-'EOF'
{
  "registry-mirrors": [
    "https://registry.docker-cn.com"
  ]
}
EOF
sudo systemctl daemon-reload
sudo systemctl restart docker
```

### Q2: 私有镜像拉取失败?

**A:** 检查:
1. 是否已登录: `docker login`
2. 用户名是否正确
3. 是否有访问权限
4. 镜像名称是否正确

### Q3: 如何删除 Docker Hub 上的镜像?

**A:**
1. 访问 https://hub.docker.com/u/your-username
2. 点击仓库名称
3. 进入 "Tags" 标签
4. 点击垃圾桶图标删除特定标签

### Q4: 免费账户的限制?

**A:** Docker Hub 免费账户:
- ✅ 无限公开仓库
- ⚠️ 1 个私有仓库
- ⚠️ 拉取限制: 100 次/6小时 (未登录), 200 次/6小时 (已登录)

如需更多私有仓库,考虑:
- Docker Hub Pro ($5/月)
- 阿里云容器镜像服务 (个人版免费)

### Q5: 数据库文件太大怎么办?

**A:** 几种方案:

```bash
# 1. 压缩数据库文件
gzip database_backup.sql
# 传输 database_backup.sql.gz

# 2. 只导出结构,不导出数据
docker exec fashion-crm-db mysqldump -u root -p"$DB_PASSWORD" --no-data "$DB_NAME" > schema_only.sql

# 3. 使用增量备份
# 只导出最近修改的数据

# 4. 分离大表
# 单独导出大表,其他表正常导出
```

---

## 📝 总结

### 推荐工作流

1. **日常开发**: 使用 Docker Hub 推送镜像
2. **数据库**: 定期导出备份到云存储
3. **部署**: 新环境拉取镜像 + 导入数据库
4. **更新**: 推送新镜像,新环境自动拉取

### 选择建议

- **多环境、频繁更新**: 使用 Docker Hub 方案 ✅
- **一次性迁移、离线环境**: 使用 Tar 文件方案
- **生产环境**: 使用私有镜像仓库 + 加密数据库

---

如有问题,请查看详细文档或联系技术支持。
