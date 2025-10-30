# Docker 部署指南 - 图像搜索引擎

本文档提供了如何使用 Docker 部署图像搜索引擎项目的完整说明。

## 前提条件

- 安装 [Docker](https://www.docker.com/get-started) (版本 20.10+)
- 安装 [Docker Compose](https://docs.docker.com/compose/install/) (版本 2.0+)
- 至少 4GB 可用内存
- 至少 10GB 可用磁盘空间

## 快速开始

### 1. 克隆仓库

```bash
git clone <repository-url>
cd image-search-engine
```

### 2. 配置环境变量

复制环境变量模板并编辑:

```bash
cp .env.example .env
```

编辑 `.env` 文件,填写必要的配置:

```bash
# 数据库配置
DB_NAME=xiangyipackage
DB_PASSWORD=your_secure_password_here

# AI API 密钥 (必填)
DASHSCOPE_API_KEY=your_dashscope_api_key_here

# OSS 配置 (可选,如不使用 OSS 可留空)
OSS_ACCESS_KEY_ID=your_oss_key
OSS_ACCESS_KEY_SECRET=your_oss_secret
OSS_ENDPOINT=oss-cn-hangzhou.aliyuncs.com
OSS_BUCKET_NAME=your_bucket_name

# 其他 API 密钥 (可选)
OPENAI_API_KEY=
DEEPSEEK_API_KEY=
```

### 3. 构建并启动容器

```bash
# 构建并启动所有服务
docker-compose up -d --build

# 查看启动日志
docker-compose logs -f
```

等待所有服务启动完成 (约 1-2 分钟)。

### 4. 访问应用

- **前端界面**: http://localhost
- **后端 API**: http://localhost:5000
- **健康检查**: http://localhost:5000/api/health

## 服务说明

### MySQL 数据库 (db)

- **镜像**: mysql:8.0
- **端口**: 3306
- **数据持久化**: `mysql_data` 卷
- **初始化脚本**: `./mysql/init.sql` (首次启动时自动执行)
- **健康检查**: mysqladmin ping

### Flask 后端 (backend)

- **基础镜像**: Python 3.11-slim (多阶段构建)
- **端口**: 5000
- **数据持久化**:
  - `upload_data` 卷 → `/app/uploads` (上传文件)
  - `vector_data` 卷 → `/app/data` (向量索引)
- **运行方式**: Gunicorn (4 workers, 2 threads)
- **健康检查**: `/api/health` 接口

### React 前端 (frontend)

- **基础镜像**: Node 18-alpine + Nginx 1.25-alpine
- **端口**: 80
- **构建工具**: Vite
- **Web 服务器**: Nginx
- **健康检查**: curl localhost

## 常用命令

### 容器管理

```bash
# 启动所有服务
docker-compose up -d

# 停止所有服务
docker-compose down

# 停止并删除数据卷 (⚠️ 会删除数据库数据)
docker-compose down -v

# 重启服务
docker-compose restart

# 重启特定服务
docker-compose restart backend
```

### 查看日志

```bash
# 查看所有服务日志
docker-compose logs -f

# 查看特定服务日志
docker-compose logs -f backend
docker-compose logs -f frontend
docker-compose logs -f db

# 查看最近 100 行日志
docker-compose logs --tail=100 backend
```

### 服务状态检查

```bash
# 查看运行状态
docker-compose ps

# 查看资源使用情况
docker stats

# 进入容器 shell
docker-compose exec backend bash
docker-compose exec db mysql -uroot -p
```

### 数据库操作

```bash
# 连接到 MySQL
docker-compose exec db mysql -uroot -p${DB_PASSWORD} xiangyipackage

# 导出数据库
docker-compose exec db mysqldump -uroot -p${DB_PASSWORD} xiangyipackage > backup.sql

# 导入数据库
docker-compose exec -T db mysql -uroot -p${DB_PASSWORD} xiangyipackage < backup.sql
```

## 数据持久化

应用使用以下 Docker 卷进行数据持久化:

| 卷名 | 挂载点 | 用途 |
|------|--------|------|
| `mysql_data` | `/var/lib/mysql` | MySQL 数据库文件 |
| `upload_data` | `/app/uploads` | 用户上传的文件 |
| `vector_data` | `/app/data` | FAISS 向量索引 |

即使删除容器,这些数据也会保留。如需完全清理:

```bash
docker-compose down -v
```

## 更新和重新部署

```bash
# 拉取最新代码
git pull

# 重新构建并启动
docker-compose up -d --build

# 查看更新日志
docker-compose logs -f
```

## 性能优化建议

### 内存分配

- **最小配置**: 4GB RAM
- **推荐配置**: 8GB+ RAM

### CPU 配置

```yaml
# 在 docker-compose.yml 中添加 CPU 限制
services:
  backend:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 2G
```

### 网络优化

内网部署时,可以配置固定 IP:

```yaml
networks:
  app-network:
    driver: bridge
    ipam:
      config:
        - subnet: 172.20.0.0/16
```

## 故障排除

### 问题 1: 后端容器启动失败

**症状**: `docker-compose ps` 显示 backend 为 `Exit 1`

**解决方法**:
```bash
# 查看详细错误日志
docker-compose logs backend

# 常见原因:
# 1. 数据库连接失败 → 检查 DB_PASSWORD 是否正确
# 2. API 密钥缺失 → 检查 .env 文件中的 DASHSCOPE_API_KEY
# 3. 端口被占用 → 修改 docker-compose.yml 中的端口映射
```

### 问题 2: 数据库连接超时

**症状**: `SQLALCHEMY.exc.OperationalError`

**解决方法**:
```bash
# 1. 确保数据库服务已完全启动
docker-compose logs db | grep "ready for connections"

# 2. 检查健康检查状态
docker-compose ps

# 3. 如果数据库未就绪,等待 30 秒后重启后端
docker-compose restart backend
```

### 问题 3: 前端无法访问后端 API

**症状**: 前端页面打开但 API 请求失败

**解决方法**:
```bash
# 1. 检查后端健康状态
curl http://localhost:5000/api/health

# 2. 检查 Nginx 配置
docker-compose exec frontend cat /etc/nginx/conf.d/default.conf

# 3. 检查网络连通性
docker-compose exec frontend ping backend
```

### 问题 4: 镜像构建失败

**症状**: `docker-compose up --build` 报错

**解决方法**:
```bash
# 1. 清理 Docker 缓存
docker system prune -a

# 2. 单独构建后端
docker-compose build --no-cache backend

# 3. 单独构建前端
docker-compose build --no-cache frontend

# 4. 如果是网络问题,可以配置 Docker 代理
```

### 问题 5: 端口冲突

**症状**: `bind: address already in use`

**解决方法**:
```bash
# 查看占用端口的进程
lsof -i :80
lsof -i :5000
lsof -i :3306

# 方法 1: 停止占用端口的服务
# 方法 2: 修改 docker-compose.yml 中的端口映射
ports:
  - "8080:80"  # 前端改为 8080
  - "5001:5000"  # 后端改为 5001
```

## 安全建议

1. **修改默认密码**: 务必在 `.env` 中设置强密码
2. **限制端口访问**: 内网部署时可以不暴露数据库端口
3. **定期备份**: 定期备份 `mysql_data` 卷
4. **日志监控**: 使用 `docker-compose logs` 监控异常

## 监控和维护

### 查看容器健康状态

```bash
# 查看健康检查状态
docker inspect fashion-crm-backend | grep -A 10 Health
docker inspect fashion-crm-frontend | grep -A 10 Health
```

### 清理日志

```bash
# Docker 日志可能会占用大量空间
# 配置日志轮转 (在 docker-compose.yml 中)
logging:
  driver: "json-file"
  options:
    max-size: "10m"
    max-file: "3"
```

## 技术支持

如遇到问题,请提供以下信息:

1. 运行环境 (OS, Docker 版本)
2. 错误日志 (`docker-compose logs`)
3. 容器状态 (`docker-compose ps`)
4. 环境变量配置 (不包含敏感信息)

---

**最后更新**: 2025-10-28
