FROM python:3.11-alpine

# smartmontools: 读 SMART；lm-sensors: 温度；util-linux: lsblk；
# e2fsprogs: df 辅助；docker-cli: docker ps（需挂宿主 docker.sock）
RUN apk add --no-cache smartmontools lm-sensors util-linux e2fsprogs docker-cli

WORKDIR /app
COPY nasdash.py index.html ./

EXPOSE 8066
VOLUME ["/app/data"]

# 容器内必须监听 0.0.0.0，否则端口映射进不来
CMD ["python", "-u", "nasdash.py", "--mode", "local", "--port", "8066", "--bind", "0.0.0.0"]
