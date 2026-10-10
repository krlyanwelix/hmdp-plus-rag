"""重排 —— 精度提升最大的一步。

Bi-Encoder vs Cross-Encoder 的本质区别：
    Bi-Encoder     问题和文档各自独立编码成向量，只在最后算一次相似度。可预计算，快。
    Cross-Encoder  问题和文档拼在一起送进模型，从第一层就做 token 级交叉注意力。准得多，但慢。

为什么不能直接用 Cross-Encoder 检索：它无法预计算文档表示，
10 万文档意味着每次查询跑 10 万次前向，不可行。所以只能两阶段：
    Bi-Encoder 召回 top 50  ->  Cross-Encoder 精排取 top 5
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from rag.retrieve.bm25 import BM25Index
from rag.types import Hit


class Reranker(ABC):
    name: str = "base"

    @abstractmethod
    def rerank(self, query: str, hits: list[Hit], top_k: int) -> list[Hit]:
        ...


class NoopReranker(Reranker):
    """关闭重排，直接截断。用于做 ablation 对比。"""

    name = "none"

    def rerank(self, query: str, hits: list[Hit], top_k: int) -> list[Hit]:
        return hits[:top_k]


class LexicalReranker(Reranker):
    """零依赖的词法重排：在候选池内用 BM25 重新打分。

    这不是语义重排，但在专有名词、型号、错误码这类场景上确实有效，
    而且比「按 RRF 排名直接截断」更关注真实的词面证据。

    实现上刻意用 BM25 而不是「重合词长度求和」这种朴素启发式 ——
    后者会系统性偏袒长文档（长块能凑出更多匹配词），
    导致真正包含答案的短段落被压下去。BM25 自带文档长度归一化，
    并用 IDF 压低「的」「是」这类到处都有的词。
    """

    name = "lexical"

    def rerank(self, query: str, hits: list[Hit], top_k: int) -> list[Hit]:
        if len(hits) <= 1:
            return hits[:top_k]

        # 在候选池内建一个临时 BM25：IDF 相对候选池计算
        pool = BM25Index().build([{"id": h.chunk_id, "text": h.text} for h in hits])
        scores = dict(pool.search(query, len(hits)))

        ordered = sorted(
            hits,
            key=lambda h: (-scores.get(h.chunk_id, 0.0), -h.score),
        )[:top_k]
        return [
            Hit(
                chunk_id=h.chunk_id,
                score=float(scores.get(h.chunk_id, 0.0)),
                text=h.text,
                metadata={**h.metadata, "rrf_score": h.score},
            )
            for h in ordered
        ]


def _resolve_device(device: str | None) -> str | None:
    """auto/None 交给 sentence-transformers 自己选（有 CUDA 用 GPU），否则原样透传。"""
    if device in (None, "", "auto"):
        return None
    return device


class CrossEncoderReranker(Reranker):
    """真实重排模型，推荐 BAAI/bge-reranker-v2-m3。"""

    name = "cross-encoder"

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3", device: str | None = None):
        from sentence_transformers import CrossEncoder  # type: ignore

        self.model = CrossEncoder(model_name, device=_resolve_device(device))
        self.model_name = model_name
        self.device = str(getattr(self.model, "device", "?"))

    def rerank(self, query: str, hits: list[Hit], top_k: int) -> list[Hit]:
        if not hits:
            return []
        pairs = [(query, h.text) for h in hits]
        scores = self.model.predict(pairs)
        order = sorted(range(len(hits)), key=lambda i: -float(scores[i]))[:top_k]
        return [
            Hit(
                chunk_id=hits[i].chunk_id,
                score=float(scores[i]),
                text=hits[i].text,
                metadata={**hits[i].metadata, "rrf_score": hits[i].score},
            )
            for i in order
        ]


def get_reranker(cfg, verbose: bool = True) -> Reranker:
    kind = (cfg.reranker or "auto").lower()
    if kind == "none":
        return NoopReranker()

    if kind in {"auto", "cross"}:
        try:
            rr = CrossEncoderReranker(cfg.rerank_model, device=cfg.device)
            if verbose:
                print(f"[reranker] {rr.model_name} (device={rr.device})")
            return rr
        except Exception as exc:
            if kind == "cross":
                raise
            if verbose:
                print(f"[reranker] Cross-Encoder 不可用（{type(exc).__name__}），"
                      f"降级为内置词法重排")

    return LexicalReranker()
