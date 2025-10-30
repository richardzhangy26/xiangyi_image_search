# 构建阶段
FROM node:18-alpine as builder

WORKDIR /app

# 复制 package.json 和 lock 文件
COPY package*.json ./

# 设置淘宝镜像源并安装依赖
RUN npm config set registry https://registry.npmmirror.com && \
    npm ci --prefer-offline --no-audit

# 复制源代码
COPY . .

# 构建应用
RUN npm run build

# 生产阶段 - Nginx
FROM nginx:1.25-alpine

# 安装 curl 用于健康检查
RUN apk add --no-cache curl

# 复制构建产物
COPY --from=builder /app/dist /usr/share/nginx/html

# 复制 nginx 配置
COPY nginx.conf /etc/nginx/conf.d/default.conf

# 创建非特权用户(生产最佳实践)
RUN chown -R nginx:nginx /usr/share/nginx/html && \
    chown -R nginx:nginx /var/cache/nginx && \
    chown -R nginx:nginx /var/log/nginx && \
    chown -R nginx:nginx /etc/nginx/conf.d && \
    touch /var/run/nginx.pid && \
    chown -R nginx:nginx /var/run/nginx.pid

# 切换到非 root 用户
USER nginx

# 暴露端口
EXPOSE 80

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost/ || exit 1

# 启动 Nginx
CMD ["nginx", "-g", "daemon off;"]
