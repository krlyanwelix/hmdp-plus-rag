"""Prompt 组装。

三个容易被忽略但影响很大的点：
  1. Lost in the Middle —— 模型对上下文的首尾记得最牢，中间容易被忽略。
     所以要把最相关的片段放在开头和结尾，而不是按分数顺序一路排下去。
  2. 上下文污染 —— 塞进一条不相关的片段，可能比不检索还糟，模型会被带偏。
     宁可少给，不要乱给。
  3. 强制引用 —— 要求标注来源编号，既是给用户的验证入口，也是你 debug 检索质量的抓手。
"""

from __future__ import annotations

import re

from rag.types import Context

SYSTEM_PROMPT = """你是一个严谨的知识库问答助手。请严格遵守以下规则：

1. 只依据「参考资料」作答，不要使用资料之外的知识，也不要凭常识推测。
2. 如果参考资料不足以回答问题，直接回答「根据现有资料无法回答该问题」，并说明缺什么。
3. 每一条结论后面用 [编号] 标注它来自哪条资料，可以标注多个。
4. 如果不同资料之间存在冲突，明确指出冲突并分别标注来源。
5. 回答简洁、结构清晰，不要复述全部资料，只提取回答问题所需的部分。
6. 用与提问相同的语言回答。"""


def reorder_lost_in_middle(contexts: list[Context]) -> list[Context]:
    """把最相关的放在首尾，次相关的放中间。

    输入按相关性降序，输出顺序为 rank0, rank2, rank4, ..., rank5, rank3, rank1，
    这样 rank0 在开头、rank1 在结尾，都处在模型的注意力高地上。
    """
    if len(contexts) <= 2:
        return list(contexts)
    head: list[Context] = []
    tail: list[Context] = []
    for i, ctx in enumerate(contexts):
        (head if i % 2 == 0 else tail).append(ctx)
    return head + tail[::-1]


def _header(index: int, ctx: Context) -> str:
    parts = [f"来源：{ctx.source or '未知'}"]
    heading = ctx.metadata.get("heading")
    if heading:
        parts.append(f"章节：{heading}")
    return f"[{index}] " + " ｜ ".join(parts)


def build_context_block(contexts: list[Context]) -> tuple[str, list[Context]]:
    """拼装参考资料文本。返回 (文本, 展示顺序的 contexts)。

    编号按**展示顺序**生成，所以引用 [1] 一定对应展示出来的第 1 条，不会错位。
    """
    ordered = reorder_lost_in_middle(contexts)
    blocks = [f"{_header(i, ctx)}\n{ctx.text.strip()}" for i, ctx in enumerate(ordered, start=1)]
    return "\n\n---\n\n".join(blocks), ordered


def build_messages(question: str, contexts: list[Context]) -> tuple[list[dict], list[Context]]:
    """返回 (messages, 展示顺序的 contexts)。"""
    if not contexts:
        block = "（没有检索到任何相关资料）"
        ordered: list[Context] = []
    else:
        block, ordered = build_context_block(contexts)

    user = f"""参考资料：

{block}

---

问题：{question}

请依据上述参考资料回答，并标注来源编号。"""
    return (
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        ordered,
    )


_CITE_RE = re.compile(r"\[(\d+)\]")


def extract_citations(answer: str, sources: list[str]) -> list[str]:
    """把答案里的 [n] 映射回来源，用于检查引用是否越界。"""
    cited: list[str] = []
    for m in _CITE_RE.finditer(answer):
        i = int(m.group(1)) - 1
        if 0 <= i < len(sources) and sources[i] not in cited:
            cited.append(sources[i])
    return cited
