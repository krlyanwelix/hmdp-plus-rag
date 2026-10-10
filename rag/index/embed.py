"""向量化层。

原理：embedding 把文本映射到高维空间，让语义相近的文本距离近。
相似度用余弦（向量归一化后等价于点积）。

一个必须记住的坑：**相似 != 相关**。
查询是「问题」，文档是「陈述」，字面不同但语义相关（非对称检索）。
BGE 系列训练时用了非对称前缀，查询端必须手动加上，否则明显掉召回。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.request
from abc import ABC, abstractmethod

import numpy as np

_CJK = r"\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af"
_WORD_RE = re.compile(r"[a-z0-9_]+")
_CJK_RUN_RE = re.compile(f"[{_CJK}]+")


def _l2norm(mat: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(mat, axis=1, keepdims=True)
    norm[norm == 0] = 1.0
    return (mat / norm).astype(np.float32)


def tokenize_for_embedding(text: str) -> list[str]:
    """英文按词，中日韩按「单字 + 相邻字 bigram」。

    中文没有空格，单字信息量太低、整句又太稀疏，bigram 是最省事的折中
    （装 jieba 后可换成真实分词，见 README 扩展点）。
    """
    text = text.lower()
    tokens = _WORD_RE.findall(text)
    for run in _CJK_RUN_RE.findall(text):
        tokens.extend(run)
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def _hash_slot(token: str, dim: int) -> tuple[int, float]:
    """哈希到 [0, dim) 并给一个 ±1 符号，符号哈希能抵消部分碰撞偏差。"""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    v = int.from_bytes(digest, "little")
    return v % dim, (1.0 if (v >> 63) & 1 else -1.0)


class Embedder(ABC):
    """统一接口：encode 返回已 L2 归一化的 (n, dim) float32 矩阵。"""

    name: str = "base"
    dim: int = 0

    @abstractmethod
    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        raise NotImplementedError

    def encode_one(self, text: str, is_query: bool = False) -> np.ndarray:
        return self.encode([text], is_query=is_query)[0]


class HashingEmbedder(Embedder):
    """零依赖降级方案：特征哈希 + 子线性 TF。

    质量明显不如真实 embedding 模型，但胜在离线可用、确定性、无需下载，
    足以验证「切块 / 混合检索 / 重排 / 引用」这些管线环节是否正确。
    生产环境请务必换成真实模型。
    """

    name = "hashing"

    def __init__(self, dim: int = 1024):
        self.dim = dim

    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            counts: dict[str, int] = {}
            for tok in tokenize_for_embedding(text):
                counts[tok] = counts.get(tok, 0) + 1
            for tok, c in counts.items():
                idx, sign = _hash_slot(tok, self.dim)
                out[i, idx] += sign * (1.0 + math.log(c))  # 子线性 TF，抑制高频词
        return _l2norm(out)


class SentenceTransformerEmbedder(Embedder):
    """真实本地模型，推荐 BAAI/bge-m3（中英混合 + 长文本友好）。"""

    name = "sentence-transformers"

    def __init__(self, model_name: str = "BAAI/bge-m3", query_prefix: str = ""):
        from sentence_transformers import SentenceTransformer  # type: ignore

        self.model = SentenceTransformer(model_name)
        self.model_name = model_name
        self.query_prefix = query_prefix
        self.dim = int(self.model.get_sentence_embedding_dimension())

    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        if is_query and self.query_prefix:
            texts = [self.query_prefix + t for t in texts]
        vecs = self.model.encode(
            texts,
            batch_size=32,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return np.asarray(vecs, dtype=np.float32)


class OpenAIEmbedder(Embedder):
    """走 OpenAI 兼容的 /v1/embeddings 接口（DeepSeek、通义、vLLM 等都兼容）。"""

    name = "openai"

    def __init__(self, model: str, base_url: str, api_key: str, timeout: int = 60, dim: int = 1024):
        if not api_key:
            raise ValueError("OpenAIEmbedder 需要 LLM_API_KEY / OPENAI_API_KEY")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.dim = dim

    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        payload = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        rows = sorted(body["data"], key=lambda d: d.get("index", 0))
        mat = np.asarray([r["embedding"] for r in rows], dtype=np.float32)
        self.dim = mat.shape[1]
        return _l2norm(mat)


def get_embedder(cfg, verbose: bool = True) -> Embedder:
    """工厂：按配置选择实现，auto 时优先真实模型、失败则降级。"""
    kind = (cfg.embedder or "auto").lower()

    if kind in {"auto", "st"}:
        try:
            emb = SentenceTransformerEmbedder(cfg.embed_model, cfg.embed_query_prefix)
            if verbose:
                print(f"[embedder] {emb.model_name} (dim={emb.dim})")
            return emb
        except Exception as exc:
            if kind == "st":
                raise
            if verbose:
                print(f"[embedder] sentence-transformers 不可用（{type(exc).__name__}），"
                      f"降级为内置 hashing embedder")
                print("           提示：pip install sentence-transformers 可显著提升检索质量")

    if kind == "openai":
        return OpenAIEmbedder(
            model=cfg.embed_model,
            base_url=cfg.llm_base_url,
            api_key=cfg.llm_api_key,
            timeout=cfg.llm_timeout,
            dim=cfg.embed_dim,
        )

    return HashingEmbedder(dim=cfg.embed_dim)
