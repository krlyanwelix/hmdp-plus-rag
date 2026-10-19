"""BM25 关键词检索 —— 补齐向量检索的固有盲区。

向量检索擅长「意思对」，但对精确符号极不敏感：
产品型号 XR-2000、错误码 E1042、人名、代码标识符，这些在语义空间里没有「意思」，
向量会把它们模糊掉。BM25 基于词频统计，恰好对精确匹配最强。

公式（Okapi BM25）：
    score(q,d) = Σ_t IDF(t) · tf·(k1+1) / (tf + k1·(1-b + b·|d|/avgdl))
IDF 用 BM25+ 变体 log(1 + (N-df+0.5)/(df+0.5))，恒为正，避免高频词得负分。
"""

from __future__ import annotations

import math
import re
from collections import defaultdict

# 连字符/下划线/点的复合词整体保留（xr-2000、upstream_timeout），同时拆出各部分
_WORD_RE = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")

try:  # 装了 jieba 就用真实分词，中文 BM25 会明显更准
    import jieba  # type: ignore

    _HAS_JIEBA = True
except Exception:  # pragma: no cover
    _HAS_JIEBA = False


def tokenize(text: str) -> list[str]:
    """英文按复合词切，中文按分词（无 jieba 时退化为单字 + bigram）。"""
    text = text.lower()
    tokens: list[str] = []

    for word in _WORD_RE.findall(text):
        tokens.append(word)
        if any(sep in word for sep in "-_."):
            tokens.extend(p for p in re.split(r"[-_.]", word) if p)

    for run in _CJK_RUN_RE.findall(text):
        if _HAS_JIEBA:
            tokens.extend(t for t in jieba.cut(run) if t.strip())
        else:
            tokens.extend(run)
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))

    return tokens


class BM25Index:
    """内存倒排索引。数据量不大时每次启动从向量库重建即可，无需持久化。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._ids: list[str] = []
        self._lens: list[int] = []
        self._postings: dict[str, list[tuple[int, int]]] = {}
        self._df: dict[str, int] = {}
        self._avgdl: float = 0.0

    def build(self, records: list[dict]) -> "BM25Index":
        """records: [{id, text, ...}]"""
        self._ids = [r["id"] for r in records]
        self._lens = []
        self._postings = {}
        self._df = {}

        for i, rec in enumerate(records):
            counts: dict[str, int] = {}
            for tok in tokenize(rec.get("text", "")):
                counts[tok] = counts.get(tok, 0) + 1
            self._lens.append(sum(counts.values()))
            for term, tf in counts.items():
                self._postings.setdefault(term, []).append((i, tf))
                self._df[term] = self._df.get(term, 0) + 1

        n = len(self._ids)
        self._avgdl = (sum(self._lens) / n) if n else 0.0
        return self

    def search(self, query: str, top_k: int) -> list[tuple[str, float]]:
        if not self._ids:
            return []
        n = len(self._ids)
        scores: dict[int, float] = defaultdict(float)

        for term in set(tokenize(query)):
            postings = self._postings.get(term)
            if not postings:
                continue
            df = self._df[term]
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            for di, tf in postings:
                dl = self._lens[di] or 1
                denom = tf + self.k1 * (1 - self.b + self.b * dl / max(self._avgdl, 1e-9))
                scores[di] += idf * tf * (self.k1 + 1) / denom

        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]
        return [(self._ids[i], float(s)) for i, s in ranked]

    def __len__(self) -> int:
        return len(self._ids)
