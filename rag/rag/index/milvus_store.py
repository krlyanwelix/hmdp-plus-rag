"""Milvus 向量库后端（本地文件模式 = milvus-lite，无需另起服务）。

为什么从 Chroma 换过来：

* Chroma 的 PersistentClient 是**单进程独占**的，服务起多副本就锁库；Milvus
  从一开始就是「存算分离」的形态，本地文件只是它最小的部署单元，同一份代码
  把 `MILVUS_URI` 换成 `http://host:19530` 就能连集群，不需要改业务代码。
* Milvus 的标量字段（`event_id`、`source`）支持**过滤下推**，`search(expr=...)`
  在索引层就把别的赛事的数据排除了，top_k 名额不会被占满 —— 这是多赛事隔离
  必须做对的一点，和 Chroma 的 `where` 等价，但表达能力强得多（支持 `and/or/in`）。
* 它的索引参数（HNSW / IVF / AUTOINDEX）是显式声明的，压测时能直接把
  `nprobe`、`ef` 这些参数暴露出来做对照实验；Chroma 把这些藏在了实现里。

代价：milvus-lite 是**单进程**的，同一时刻只能有一个进程打开同一个 db 文件。
多副本部署必须换成独立 Milvus（`MILVUS_URI=http://...`）。

集合 schema 是固定的（Milvus 要求建集合时就确定字段和向量维度）：
    id        VARCHAR 主键（就是 chunk_id）
    vector    FLOAT_VECTOR(dim)
    text      VARCHAR(8192)   子块原文
    source    VARCHAR(512)    来源文件名，用于按文档删除
    parent_id VARCHAR(64)     父块 id，small-to-big 回溯用
    event_id  INT64           赛事 id，过滤下推用
    meta      VARCHAR(8192)   其余 metadata 的 JSON 原样存，保证不丢字段
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from rag.index.store import VectorStore, _match_meta
from rag.types import Hit

# 只有这几个字段进了 Milvus 的标量列，可以下推成布尔表达式；
# 其余 metadata（如 heading）留在 meta JSON 里，检索后本地过滤。
_SCALAR_FIELDS = {"source", "parent_id", "event_id"}

_MAX_VARCHAR = 8192


def _lit(value: Any) -> str:
    """把 Python 标量转成 Milvus 表达式字面量。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


class MilvusStore(VectorStore):
    backend = "milvus"

    def __init__(
        self,
        uri: str | Path = "data/milvus.db",
        collection: str = "rag_chunks",
        metric: str = "COSINE",
    ):
        from pymilvus import MilvusClient  # type: ignore

        raw = str(uri)
        # http(s):// 表示连独立服务；否则当本地文件（milvus-lite）
        if not raw.startswith(("http://", "https://", "tcp://")):
            path = Path(raw)
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = str(path)
        self.uri = raw
        self.collection = collection
        self.metric = metric.upper()
        self.client = MilvusClient(self.uri)
        self._dim = 0
        self._ensure_collection()

    # ------------------------------------------------------------------ #
    # 集合管理
    # ------------------------------------------------------------------ #
    def _ensure_collection(self) -> None:
        if not self.client.has_collection(self.collection):
            return
        info = self.client.describe_collection(self.collection)
        for field in info.get("fields", []):
            params = field.get("params") or {}
            if field.get("name") == "vector":
                self._dim = int(params.get("dim") or 0)
        try:
            self.client.load_collection(self.collection)
        except Exception:
            # 已加载会抛「already loaded」，忽略即可
            pass

    def _create_collection(self, dim: int) -> None:
        from pymilvus import DataType  # type: ignore

        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=int(dim))
        schema.add_field("text", DataType.VARCHAR, max_length=_MAX_VARCHAR)
        schema.add_field("source", DataType.VARCHAR, max_length=512)
        schema.add_field("parent_id", DataType.VARCHAR, max_length=64)
        schema.add_field("event_id", DataType.INT64)
        schema.add_field("meta", DataType.VARCHAR, max_length=_MAX_VARCHAR)

        index = self.client.prepare_index_params()
        index.add_index(field_name="vector", index_type="AUTOINDEX", metric_type=self.metric)
        self.client.create_collection(self.collection, schema=schema, index_params=index)
        self.client.load_collection(self.collection)
        self._dim = int(dim)

    # ------------------------------------------------------------------ #
    # 行 <-> 记录
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_row(rec: dict[str, Any]) -> dict[str, Any]:
        meta = dict(rec.get("metadata") or {})
        # 标量列与 meta JSON 二选一存储：标量列供下推，meta 保留完整信息
        for key in _SCALAR_FIELDS:
            meta.pop(key, None)
        return {
            "id": str(rec["id"]),
            "vector": [float(x) for x in rec["embedding"]],
            "text": (rec.get("text") or "")[:_MAX_VARCHAR],
            "source": str((rec.get("metadata") or {}).get("source") or "")[:512],
            "parent_id": str((rec.get("metadata") or {}).get("parent_id") or "")[:64],
            "event_id": int((rec.get("metadata") or {}).get("event_id") or 0),
            "meta": json.dumps(meta, ensure_ascii=False)[:_MAX_VARCHAR],
        }

    @staticmethod
    def _to_record(row: dict[str, Any]) -> dict[str, Any]:
        try:
            meta = json.loads(row.get("meta") or "{}")
        except json.JSONDecodeError:
            meta = {}
        for key in _SCALAR_FIELDS:
            value = row.get(key)
            if value not in (None, ""):
                meta[key] = value
        return {"id": row["id"], "text": row.get("text") or "", "metadata": meta}

    def _expr(self, where: dict | None) -> tuple[str, dict]:
        """返回 (下推表达式, 需要在本地复核的条件)。"""
        if not where:
            return "", {}
        pushed, rest = [], {}
        for key, value in where.items():
            if key in _SCALAR_FIELDS:
                pushed.append(f"{key} == {_lit(value)}")
            else:
                rest[key] = value
        return (" and ".join(pushed), rest)

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def add(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        dim = len(records[0]["embedding"])
        if not self.client.has_collection(self.collection):
            self._create_collection(dim)
        elif not self._dim:
            self._ensure_collection()
        if self._dim and dim != self._dim:
            raise ValueError(f"向量维度不一致：集合是 {self._dim}，本批是 {dim}")

        rows = [self._to_row(r) for r in records]
        for start in range(0, len(rows), 256):
            self.client.insert(self.collection, rows[start : start + 256])
        # 让刚写的数据立刻可搜（lite 模式下不 flush 也能搜到，这里显式一点更稳）
        try:
            self.client.flush(self.collection)
        except Exception:
            pass

    def delete_by_source(self, source: str) -> int:
        if not self.client.has_collection(self.collection):
            return 0
        before = self.count()
        self.client.delete(self.collection, filter=f"source == {_lit(source)}")
        return max(0, before - self.count())

    def reset(self) -> None:
        if self.client.has_collection(self.collection):
            self.client.drop_collection(self.collection)
        self._dim = 0
        self._ensure_collection()

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def search(self, query_vec: np.ndarray, top_k: int, where: dict | None = None) -> list[Hit]:
        if not self.client.has_collection(self.collection):
            return []
        expr, rest = self._expr(where)
        # 有本地复核条件时多要一些候选，避免过滤后不够数
        limit = min(max(top_k * 3, top_k), 1000) if rest else top_k
        q = np.asarray(query_vec, dtype=np.float32).reshape(-1)
        try:
            res = self.client.search(
                self.collection,
                data=[[float(x) for x in q]],
                limit=limit,
                filter=expr or "",
                output_fields=["text", "source", "parent_id", "event_id", "meta"],
            )
        except Exception as exc:
            raise RuntimeError(f"Milvus 检索失败：{exc}") from exc

        hits: list[Hit] = []
        for row in (res[0] if res else []):
            entity = dict(row.get("entity") or {})
            entity.setdefault("id", row.get("id"))
            record = self._to_record(entity)
            if rest and not _match_meta(record["metadata"], rest):
                continue
            hits.append(
                Hit(
                    chunk_id=record["id"],
                    # COSINE/IP 下 Milvus 返回的就是相似度；L2 下是距离，需要取负
                    score=float(row.get("distance", 0.0)) if self.metric != "L2"
                    else -float(row.get("distance", 0.0)),
                    text=record["text"],
                    metadata=record["metadata"],
                )
            )
        return hits[:top_k]

    def get(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids or not self.client.has_collection(self.collection):
            return {}
        rows = self.client.get(
            self.collection,
            ids=[str(i) for i in ids],
            output_fields=["text", "source", "parent_id", "event_id", "meta"],
        )
        out: dict[str, dict[str, Any]] = {}
        for row in rows or []:
            record = self._to_record(row)
            out[record["id"]] = record
        return out

    def all_records(self) -> list[dict[str, Any]]:
        if not self.client.has_collection(self.collection):
            return []
        out: list[dict[str, Any]] = []
        # query_iterator 比一次性 query 稳：不受单次返回条数上限影响
        iterator = self.client.query_iterator(
            self.collection,
            filter='id != ""',
            output_fields=["id", "text", "source", "parent_id", "event_id", "meta"],
            batch_size=1000,
        )
        while True:
            batch = iterator.next()
            if not batch:
                break
            out.extend(self._to_record(row) for row in batch)
        iterator.close()
        return out

    def count(self) -> int:
        if not self.client.has_collection(self.collection):
            return 0
        try:
            stat = self.client.get_collection_stats(self.collection)
            return int(stat.get("row_count") or 0)
        except Exception:
            return 0

    def persist(self) -> None:
        # Milvus 写入即持久化，无需显式落盘
        return None

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass
