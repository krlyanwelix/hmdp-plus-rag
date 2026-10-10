"""向量库。

两个后端，同一接口，配置项 STORE_BACKEND 切换：
    memory  numpy 矩阵暴力检索 —— 精确、零依赖、几万条以内够用
    chroma  Chroma PersistentClient + HNSW —— 近似检索，可扩展

为什么 ANN 是「近似」的：HNSW 用分层图把查询复杂度降到约 O(log n)，
代价是会漏掉少量真正相关的项。所以召回时 top_k 要开大，精度交给重排。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np

from rag.types import Hit


class VectorStore(ABC):
    """只存「参与检索的子块」。父块由 ParentStore 单独管理（见 parents.py）。"""

    backend: str = "base"

    @abstractmethod
    def add(self, records: list[dict[str, Any]]) -> None:
        """records: [{id, text, embedding: list[float], metadata: dict}]"""

    @abstractmethod
    def search(self, query_vec: np.ndarray, top_k: int) -> list[Hit]:
        ...

    @abstractmethod
    def get(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        ...

    @abstractmethod
    def all_records(self) -> list[dict[str, Any]]:
        """全量取出，用于构建 BM25 索引。"""

    @abstractmethod
    def count(self) -> int:
        ...

    @abstractmethod
    def delete_by_source(self, source: str) -> int:
        ...

    @abstractmethod
    def reset(self) -> None:
        ...

    def persist(self) -> None:  # 有的后端是即时落盘的，默认无操作
        return None


# --------------------------------------------------------------------------- #
# 后端一：numpy 暴力检索（零依赖，默认）
# --------------------------------------------------------------------------- #
class MemoryStore(VectorStore):
    """精确检索全部向量。数据量上万后延迟线性增长，届时换 Chroma。"""

    backend = "memory"

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._vec_file = self.path / "vectors.npy"
        self._meta_file = self.path / "records.json"
        self._ids: list[str] = []
        self._texts: list[str] = []
        self._metas: list[dict[str, Any]] = []
        self._index: dict[str, int] = {}
        self._mat: np.ndarray | None = None
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        if not (self._meta_file.exists() and self._vec_file.exists()):
            return
        try:
            payload = json.loads(self._meta_file.read_text(encoding="utf-8"))
            mat = np.load(self._vec_file)
        except Exception as exc:
            print(f"[store] 索引文件损坏，已忽略：{exc}")
            return
        self._ids = list(payload.get("ids", []))
        self._texts = list(payload.get("texts", []))
        self._metas = list(payload.get("metadatas", []))
        self._index = {cid: i for i, cid in enumerate(self._ids)}
        self._mat = mat.astype(np.float32) if mat.size else None

    def persist(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self._meta_file.write_text(
            json.dumps(
                {"ids": self._ids, "texts": self._texts, "metadatas": self._metas},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if self._mat is not None and self._mat.size:
            np.save(self._vec_file, self._mat)
        elif self._vec_file.exists():
            self._vec_file.unlink()

    # ---- 写 ----
    def add(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        vecs = np.asarray([r["embedding"] for r in records], dtype=np.float32)
        for rec, vec in zip(records, vecs):
            cid = rec["id"]
            if cid in self._index:  # upsert：同 id 覆盖
                i = self._index[cid]
                self._texts[i] = rec["text"]
                self._metas[i] = rec.get("metadata", {})
                self._mat[i] = vec
            else:
                self._index[cid] = len(self._ids)
                self._ids.append(cid)
                self._texts.append(rec["text"])
                self._metas.append(rec.get("metadata", {}))
                self._mat = vec[None, :] if self._mat is None else np.vstack([self._mat, vec[None, :]])

    def delete_by_source(self, source: str) -> int:
        keep = [i for i, m in enumerate(self._metas) if m.get("source") != source]
        removed = len(self._ids) - len(keep)
        if not removed:
            return 0
        self._ids = [self._ids[i] for i in keep]
        self._texts = [self._texts[i] for i in keep]
        self._metas = [self._metas[i] for i in keep]
        self._mat = self._mat[keep] if self._mat is not None and len(keep) else None
        self._index = {cid: i for i, cid in enumerate(self._ids)}
        return removed

    def reset(self) -> None:
        self._ids, self._texts, self._metas, self._index = [], [], [], {}
        self._mat = None
        for f in (self._meta_file, self._vec_file):
            if f.exists():
                f.unlink()

    # ---- 读 ----
    def search(self, query_vec: np.ndarray, top_k: int) -> list[Hit]:
        if self._mat is None or not self._ids:
            return []
        q = np.asarray(query_vec, dtype=np.float32).reshape(-1)
        sims = self._mat @ q  # 已归一化，点积即余弦
        k = min(top_k, len(self._ids))
        order = np.argpartition(-sims, k - 1)[:k]
        order = order[np.argsort(-sims[order])]
        return [
            Hit(
                chunk_id=self._ids[i],
                score=float(sims[i]),
                text=self._texts[i],
                metadata=self._metas[i],
            )
            for i in order
        ]

    def get(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for cid in ids:
            i = self._index.get(cid)
            if i is not None:
                out[cid] = {"id": cid, "text": self._texts[i], "metadata": self._metas[i]}
        return out

    def all_records(self) -> list[dict[str, Any]]:
        return [
            {"id": cid, "text": self._texts[i], "metadata": self._metas[i]}
            for i, cid in enumerate(self._ids)
        ]

    def count(self) -> int:
        return len(self._ids)


# --------------------------------------------------------------------------- #
# 后端二：Chroma（HNSW 近似检索）
# --------------------------------------------------------------------------- #
class _NoEmbeddingFunction:
    """占位 embedding function。

    本项目始终显式传入向量，但 Chroma 的集合会带一个默认 EF，
    某些版本会因此在首次使用时联网下载 ONNX 模型。用这个哑实现堵住它。
    """

    def __call__(self, input):  # noqa: A002
        raise RuntimeError("本项目显式传入 embedding，不应调用集合自带的 embedding function")

    def name(self) -> str:
        return "none"

    @staticmethod
    def build_from_config(config):  # noqa: ARG004
        return _NoEmbeddingFunction()

    def get_config(self) -> dict:
        return {}

    def default_space(self) -> str:
        return "cosine"

    def supported_spaces(self) -> list[str]:
        return ["cosine", "l2", "ip"]


class ChromaStore(VectorStore):
    """Chroma 持久化集合，底层 HNSW，距离度量 cosine。"""

    backend = "chroma"

    def __init__(self, path: Path | str, collection: str = "rag"):
        import chromadb  # type: ignore

        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(self.path))
        self.collection = self.client.get_or_create_collection(
            name=collection,
            embedding_function=_NoEmbeddingFunction(),
            metadata={"hnsw:space": "cosine"},
        )

    def add(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        self.collection.upsert(
            ids=[r["id"] for r in records],
            documents=[r["text"] for r in records],
            embeddings=[list(map(float, r["embedding"])) for r in records],
            metadatas=[r.get("metadata", {}) for r in records],
        )

    def search(self, query_vec: np.ndarray, top_k: int) -> list[Hit]:
        n = self.count()
        if not n:
            return []
        q = np.asarray(query_vec, dtype=np.float32).reshape(-1)
        res = self.collection.query(
            query_embeddings=[list(map(float, q))],
            n_results=min(top_k, n),
            include=["documents", "metadatas", "distances"],
        )
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        hits: list[Hit] = []
        for i, cid in enumerate(ids):
            dist = float(dists[i]) if i < len(dists) else 1.0
            hits.append(
                Hit(
                    chunk_id=cid,
                    score=1.0 - dist,  # cosine 空间下 distance = 1 - similarity
                    text=docs[i] if i < len(docs) else "",
                    metadata=metas[i] if i < len(metas) else {},
                )
            )
        return hits

    def get(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        res = self.collection.get(ids=list(ids), include=["documents", "metadatas"])
        out: dict[str, dict[str, Any]] = {}
        for i, cid in enumerate(res.get("ids") or []):
            docs = res.get("documents") or []
            metas = res.get("metadatas") or []
            out[cid] = {
                "id": cid,
                "text": docs[i] if i < len(docs) else "",
                "metadata": metas[i] if i < len(metas) else {},
            }
        return out

    def all_records(self) -> list[dict[str, Any]]:
        res = self.collection.get(include=["documents", "metadatas"])
        out = []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        for i, cid in enumerate(res.get("ids") or []):
            out.append(
                {
                    "id": cid,
                    "text": docs[i] if i < len(docs) else "",
                    "metadata": metas[i] if i < len(metas) else {},
                }
            )
        return out

    def count(self) -> int:
        return int(self.collection.count())

    def delete_by_source(self, source: str) -> int:
        before = self.count()
        self.collection.delete(where={"source": source})
        return before - self.count()

    def reset(self) -> None:
        name = self.collection.name
        try:
            self.client.delete_collection(name)
        except Exception:
            pass
        self.collection = self.client.get_or_create_collection(
            name=name,
            embedding_function=_NoEmbeddingFunction(),
            metadata={"hnsw:space": "cosine"},
        )


def get_store(cfg, verbose: bool = True) -> VectorStore:
    """工厂：auto 时优先 Chroma，不可用则退回 numpy 后端。"""
    kind = (cfg.store_backend or "auto").lower()

    if kind in {"auto", "chroma"}:
        try:
            store = ChromaStore(cfg.store_path)
            if verbose:
                print(f"[store] chromadb (HNSW, cosine) @ {cfg.store_path}")
            return store
        except Exception as exc:
            if kind == "chroma":
                raise
            if verbose:
                print(f"[store] chromadb 不可用（{type(exc).__name__}），降级为 numpy 暴力检索")
                print("        提示：pip install chromadb 可切换到 HNSW 近似检索")

    return MemoryStore(cfg.store_path)
