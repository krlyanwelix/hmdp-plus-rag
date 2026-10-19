# 赛事助手（RAG）镜像
#
# 两条命令里的第二条：
#   docker compose -f rag/deploy/docker-compose.yml up -d --build
#
# 说明：
# - 不复用本地 .pylibs（那是 Windows 的 cp312 轮子，Linux 容器里用不了），
#   照 requirements.txt 重新装一遍；依赖都是纯 Python 或 manylinux 轮子，编译很快。
# - 语料（data/sports）与评测集（eval/）打包进镜像，索引在容器启动时按需重建。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl 给 entrypoint 探活用
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

EXPOSE 8090

ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
