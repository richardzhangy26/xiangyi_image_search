FROM mysql:8.0

# 将初始化脚本包含在镜像中，容器首次启动时会执行
COPY *.sql /docker-entrypoint-initdb.d/
