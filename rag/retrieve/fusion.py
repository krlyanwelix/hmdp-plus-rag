"""RRF（Reciprocal Rank Fusion）—— 融合多路召回结果。

    RRF(d) = Σ_i 1 / (k + rank_i(d))

为什么用 RRF 而不是加权求和：向量相似度（0~1）和 BM25 分数（0~几十）量纲完全不可比，
归一化又依赖分布、对异常值敏感。RRF 只看排名不看分数，天然规避了这个问题，
几乎不需要调参（k 取 60 是文献里的通用值）。
"""

from __future__ import annotations


def rrf(
    rankings: list[list[str]],
    k: int = 60,
    weights: list[float] | None = None,
) -> list[tuple[str, float]]:
    """rankings: 多路召回，每路是按相关性降序排好的 id 列表。"""
    if weights is None:
        weights = [1.0] * len(rankings)

    scores: dict[str, float] = {}
    for ranking, weight in zip(rankings, weights):
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + rank + 1)

    return sorted(scores.items(), key=lambda kv: -kv[1])
