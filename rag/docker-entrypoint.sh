#!/bin/sh
# 赛事助手容器入口：等 Milvus -> 按需建索引 -> 起 FastAPI
set -e

cd /app

MILVUS_HEALTH="${MILVUS_HEALTH_URL:-http://milvus:9091/healthz}"
echo "[entrypoint] 等待 Milvus: ${MILVUS_HEALTH}"
i=0
while [ "$i" -lt 60 ]; do
    if curl -sf "$MILVUS_HEALTH" >/dev/null 2>&1; then
        echo "[entrypoint] Milvus 就绪（第 ${i} 轮探测）"
        break
    fi
    i=$((i + 1))
    sleep 2
done

# 索引在 Milvus 里，容器重启后不用重建；空集合或 RAG_INGEST_ON_START=force 才重建
if [ "${RAG_INGEST_ON_START:-1}" != "0" ]; then
    if [ "${RAG_INGEST_ON_START}" = "force" ] || ! python deploy/ensure_index.py; then
        echo "[entrypoint] 开始建索引（8 篇语料，需要调 embedding API）"
        python cli.py ingest --rebuild
    fi
fi

echo "[entrypoint] 启动 FastAPI"
exec python service/run.py
