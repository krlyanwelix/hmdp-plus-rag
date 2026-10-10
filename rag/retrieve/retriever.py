"""检索编排：向量召回 + BM25 召回 -> RRF 融合 -> 重排 -> small-to-big 展开。

流程里每一步的顺序都是有理由的，不要随意调换：
  1. 两路召回都取较宽的 top_k（默认 30），把精度问题留给后面的重排
  2. RRF 融合 —— 只看排名，规避两路分数量纲不可比的问题
  3. Cross-Encoder 精排 —— 用交互式打分挑出真正相关的少量块
  4. 用命中的子块找到父块 —— 检索要小块（准），喂给 LLM 要大块（全）
"""

from __future__ import annotations

from rag.index.embed import Embedder
from rag.index.parents import ParentStore
from rag.index.store import VectorStore
from rag.retrieve.bm25 import BM25Index
from rag.retrieve.fusion import rrf
from rag.retrieve.rerank import Reranker, get_reranker
from rag.types import Context, Hit

MODES = ("hybrid", "vector", "bm25")


class Retriever:
    def __init__(
        self,
        cfg,
        embedder: Embedder,
        store: VectorStore,
        parents: ParentStore,
        reranker: Reranker | None = None,
        verbose: bool = True,
    ):
        self.cfg = cfg
        self.embedder = embedder
        self.store = store
        self.parents = parents
        self.reranker = reranker or get_reranker(cfg, verbose=verbose)
        self._bm25: BM25Index | None = None

    def invalidate(self) -> None:
        """索引变更后调用，强制下次检索重建 BM25。"""
        self._bm25 = None

    def _bm25_index(self) -> BM25Index:
        if self._bm25 is None:
            self._bm25 = BM25Index().build(self.store.all_records())
        return self._bm25

    # ------------------------------------------------------------------ #
    # 召回 + 融合 + 重排
    # ------------------------------------------------------------------ #
    def search(
        self,
        question: str,
        mode: str = "hybrid",
        top_k: int | None = None,
        debug: dict | None = None,
    ) -> list[Hit]:
        """返回重排后的子块命中，用于查看「到底检索到了什么」。"""
        mode = mode if mode in MODES else "hybrid"
        top_k = top_k or self.cfg.top_k_final
        rankings: list[list[str]] = []
        hits: dict[str, Hit] = {}
        stats = {"vector": 0, "bm25": 0, "fused": 0}

        # 向量召回放 rankings[0]，BM25 放后面，等价于给向量略高的先验权重
        if mode in {"hybrid", "vector"}:
            qv = self.embedder.encode_one(question, is_query=True)
            vec_hits = self.store.search(qv, self.cfg.top_k_recall)
            for h in vec_hits:
                hits[h.chunk_id] = h
            if vec_hits:
                rankings.append([h.chunk_id for h in vec_hits])
            stats["vector"] = len(vec_hits)

        if mode in {"hybrid", "bm25"}:
            bm_ranked = self._bm25_index().search(question, self.cfg.top_k_recall)
            if bm_ranked:
                rankings.append([cid for cid, _ in bm_ranked])
                stats["bm25"] = len(bm_ranked)
                # BM25 命中的块可能没进向量召回，补一次取文本
                missing = [cid for cid, _ in bm_ranked if cid not in hits]
                if missing:
                    for cid, rec in self.store.get(missing).items():
                        hits[cid] = Hit(cid, 0.0, rec["text"], rec["metadata"])

        if not rankings:
            if debug is not None:
                debug.update({**stats, "fused": 0, "reranked": 0})
            return []

        fused = rrf(rankings, k=self.cfg.rrf_k)
        stats["fused"] = len(fused)
        candidates = [
            Hit(cid, float(score), hits[cid].text, hits[cid].metadata)
            for cid, score in fused
            if cid in hits
        ]

        top = self.reranker.rerank(question, candidates, top_k)
        if debug is not None:
            debug.update({**stats, "reranked": len(top)})
        return top

    # ------------------------------------------------------------------ #
    # 完整检索 -> 交给 LLM 的上下文
    # ------------------------------------------------------------------ #
    def retrieve(
        self,
        question: str,
        mode: str = "hybrid",
        debug: dict | None = None,
    ) -> list[Context]:
        top = self.search(question, mode=mode, debug=debug)

        # small-to-big：命中子块 -> 取回父块，同一父块只保留最佳得分
        parent_order: list[str] = []
        parent_score: dict[str, float] = {}
        for hit in top:
            pid = hit.metadata.get("parent_id") or hit.chunk_id
            if pid not in parent_score:
                parent_order.append(pid)
                parent_score[pid] = hit.score

        contexts = self.parents.to_contexts(parent_order, parent_score)

        # 父块缺失时退回子块本身，保证不会因为索引不一致而丢上下文
        got = {c.metadata.get("parent_id") for c in contexts}
        for hit in top:
            pid = hit.metadata.get("parent_id") or hit.chunk_id
            if pid in got:
                continue
            contexts.append(
                Context(
                    text=hit.text,
                    source=hit.metadata.get("source", ""),
                    score=hit.score,
                    metadata=hit.metadata,
                )
            )

        if debug is not None:
            debug["contexts"] = len(contexts)
        return contexts
