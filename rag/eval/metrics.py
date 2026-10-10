"""检索层评测。

为什么必须先测检索：RAG 的上限由「能否检索到正确片段」决定。
检索不到，后面 prompt 写得再好也没用。所以 Recall@k 是第一优先指标。

这里用「来源文件级」的相关性标注 —— 最省事的标注粒度，不需要标到 chunk id。
缺点是无法区分同一文件内部的细粒度召回，作为骨架足够；
要更精细就把 relevant 换成 chunk_id 列表，函数签名不用改。

生成层指标（RAGAS 的 faithfulness / answer relevancy）需要一个 LLM 当裁判，
骨架里不内置，见 README「扩展点」。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from rag.types import Context, Hit


def load_dataset(path: Path | str) -> list[dict]:
    """每行一个 JSON：
    {"question": "...", "relevant_sources": ["a.md"], "gold_answer": "..."}
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"评测集不存在：{path}")
    items: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno} 不是合法 JSON：{exc}") from exc
    return items


def unique_sources(items: list[Context] | list[Hit]) -> list[str]:
    """按检索顺序提取去重后的来源列表（保持顺序，NDCG 需要顺序信息）。"""
    out: list[str] = []
    for it in items:
        src = it.source if isinstance(it, Context) else it.metadata.get("source", "")
        if src and src not in out:
            out.append(src)
    return out


def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """正确来源有没有进前 k —— 最先要保证的指标。"""
    if not relevant:
        return 0.0
    hit = len(set(retrieved[:k]) & relevant)
    return hit / len(relevant)


def reciprocal_rank(retrieved: list[str], relevant: set[str]) -> float:
    """第一个正确结果排名的倒数，衡量「排得够不够靠前」。"""
    for i, src in enumerate(retrieved):
        if src in relevant:
            return 1.0 / (i + 1)
    return 0.0


def ndcg_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """二值相关性下的 NDCG@k。"""
    if not relevant:
        return 0.0
    dcg = sum(
        1.0 / math.log2(i + 2) for i, src in enumerate(retrieved[:k]) if src in relevant
    )
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


def evidence_rank(contexts: list[Context], evidence: list[str]) -> int:
    """第一个「同时包含全部证据要点」的上下文序号，1-based。

    返回 -1 表示该题没有标注证据（如负样本），不参与统计。
    返回 0 表示检索结果里根本没有这段证据。
    这个指标对排序敏感 —— 证据排在第 1 段还是第 4 段，分数完全不同，
    比来源文件级的 recall 敏感得多，是骨架里最值得盯的检索指标。
    """
    if not evidence:
        return -1
    for i, ctx in enumerate(contexts, start=1):
        if all(e in ctx.text for e in evidence):
            return i
    return 0


def evidence_hit(contexts: list[Context], evidence: list[str]) -> tuple[bool, list[str]]:
    """检索到的上下文里是否包含全部答案要点（不关心排第几）。"""
    if not evidence:
        return True, []
    blob = "\n".join(c.text for c in contexts)
    missing = [e for e in evidence if e not in blob]
    return (not missing), missing


def evaluate(
    pipeline,
    dataset: list[dict],
    k: int = 5,
    mode: str = "hybrid",
    verbose: bool = True,
) -> dict:
    """跑一遍评测集，返回聚合指标与逐题明细。

    mode 参数是关键：用 vector / bm25 / hybrid 各跑一遍，
    就能定量回答「混合检索到底有没有用」这个问题。
    """
    rows: list[dict] = []
    for item in dataset:
        question = item["question"]
        relevant = set(item.get("relevant_sources") or [])
        evidence = list(item.get("evidence") or [])

        contexts = pipeline.retriever.retrieve(question, mode=mode)
        retrieved = unique_sources(contexts)
        rank = evidence_rank(contexts, evidence)
        hit, missing = evidence_hit(contexts, evidence)

        row = {
            "question": question,
            "expected": sorted(relevant),
            "retrieved": retrieved[:k],
            "evidence_rank": rank,
            "evidence_hit": hit,
            "missing_evidence": missing,
            "recall": recall_at_k(retrieved, relevant, k),
            "rr": reciprocal_rank(retrieved, relevant),
            "ndcg": ndcg_at_k(retrieved, relevant, k),
        }
        rows.append(row)
        if verbose:
            if rank < 0:
                print(f"  [负样本] {question[:34]}")
            elif rank == 1:
                print(f"  [第1段] {question[:34]}")
            elif rank > 1:
                print(f"  [第{rank}段] {question[:34]}")
            else:
                print(f"  [未命中] {question[:34]}")
                if missing:
                    print(f"          缺少要点：{missing}")

    # 只对标注了证据的题统计证据类指标；负样本单独计数
    judged = [r for r in rows if r["evidence_rank"] >= 0]
    negatives = [r for r in rows if r["evidence_rank"] < 0]
    m = len(judged) or 1
    n = len(rows) or 1

    return {
        "mode": mode,
        "k": k,
        "n": len(rows),
        "n_judged": len(judged),
        "n_negative": len(negatives),
        f"recall@{k}": sum(r["recall"] for r in rows) / n,
        "证据@1": sum(1.0 for r in judged if r["evidence_rank"] == 1) / m,
        "证据MRR": sum((1.0 / r["evidence_rank"]) if r["evidence_rank"] > 0 else 0.0 for r in judged) / m,
        "证据召回": sum(1.0 for r in judged if r["evidence_rank"] > 0) / m,
        "mrr": sum(r["rr"] for r in rows) / n,
        f"ndcg@{k}": sum(r["ndcg"] for r in rows) / n,
        "details": rows,
    }
