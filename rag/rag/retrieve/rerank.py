"""重排 —— 精度提升最大的一步。

Bi-Encoder vs Cross-Encoder 的本质区别：
    Bi-Encoder     问题和文档各自独立编码成向量，只在最后算一次相似度。可预计算，快。
    Cross-Encoder  问题和文档拼在一起送进模型，从第一层就做 token 级交叉注意力。准得多，但慢。

为什么不能直接用 Cross-Encoder 检索：它无法预计算文档表示，
10 万文档意味着每次查询跑 10 万次前向，不可行。所以只能两阶段：
    Bi-Encoder 召回 top 50  ->  Cross-Encoder 精排取 top 5
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
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


class APIReranker(Reranker):
    """走 /rerank 接口的重排器（硅基流动、Cohere 风格，非 OpenAI /embeddings 格式）。

    请求：POST {base_url}/rerank
      {"model": ..., "query": ..., "documents": [...], "top_n": N}
    响应：{"results": [{"index": i, "relevance_score": s}, ...]}

    与 embedding 不同，/rerank 是 query 与 documents 交互打分（一次网络往返搞定整个候选集）。
    内置 429 重试 + 退避，与 OpenAIEmbedder 一致。
    """

    name = "api-reranker"

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        timeout: int = 60,
        retries: int = 6,
        retry_delay: float = 2.0,
    ):
        if not api_key:
            raise ValueError("APIReranker 需要 API key")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self.device = "api"

    def rerank(self, query: str, hits: list[Hit], top_k: int) -> list[Hit]:
        if not hits:
            return []

        payload = json.dumps(
            {
                "model": self.model,
                "query": query,
                "documents": [h.text for h in hits],
                "top_n": min(top_k, len(hits)),
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}

        body = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(
                f"{self.base_url}/rerank", data=payload, headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < self.retries:
                    delay = self._retry_after(exc, attempt)
                    print(f"    [reranker] 429 限流，{delay:.1f}s 后重试", flush=True)
                    time.sleep(delay)
                    continue
                raise

        assert body is not None
        scored = {r["index"]: float(r["relevance_score"]) for r in body["results"]}
        ordered = sorted(scored, key=lambda i: -scored[i])
        return [
            Hit(
                chunk_id=hits[idx].chunk_id,
                score=scored[idx],
                text=hits[idx].text,
                metadata={**hits[idx].metadata, "rrf_score": hits[idx].score},
            )
            for idx in ordered[:top_k]
        ]

    @staticmethod
    def _retry_after(exc: urllib.error.HTTPError, attempt: int) -> float:
        ra = (exc.headers or {}).get("Retry-After")
        if ra:
            try:
                return max(0.0, float(ra))
            except (ValueError, TypeError):
                pass
        return 2.0 * (2 ** attempt)


def get_reranker(cfg, verbose: bool = True) -> Reranker:
    kind = (cfg.reranker or "auto").lower()
    if kind == "none":
        return NoopReranker()

    if kind == "api":
        base_url = cfg.rerank_base_url or cfg.embed_base_url or cfg.llm_base_url
        api_key = cfg.rerank_api_key or cfg.embed_api_key or cfg.llm_api_key
        if verbose:
            print(f"[reranker] API /rerank：model={cfg.rerank_model} @ {base_url}")
        return APIReranker(
            model=cfg.rerank_model,
            base_url=base_url,
            api_key=api_key,
            timeout=cfg.llm_timeout,
        )

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
