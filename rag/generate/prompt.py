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

REFUSAL_TEXT = "根据现有赛事规程无法回答该问题：没有检索到与提问相关的内容。建议换一种问法，或咨询组委会。"

USER_FACT_HEADER = "【当前登录用户的档案】（由业务系统按会话身份注入，只描述该用户本人）"

USER_FACT_RULES = """使用档案的规则：
1. 档案只能用于回答「该用户本人」的问题，不得用来推断其他参赛者的情况。
2. 如果问题在问别人（例如点名询问某个姓名或证件的报名情况），直接回答「只能查询你本人的报名信息」。
3. 引用档案得出的结论，在句末标注（档案），不要使用 [编号]。"""


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


def _user_fact_block(user_facts: list[str]) -> str:
    lines = [USER_FACT_HEADER]
    for fact in user_facts:
        lines.append(f"- {fact.strip()[:200]}")
    lines.append("")
    lines.append(USER_FACT_RULES)
    return "\n".join(lines)


def build_messages(
    question: str,
    contexts: list[Context],
    user_facts: list[str] | None = None,
) -> tuple[list[dict], list[Context]]:
    """返回 (messages, 展示顺序的 contexts)。

    user_facts：当前会话用户的私有数据（由 Java 从库里查出来注入），**不来自检索**。
    私有数据不进向量库是有意为之：一旦进库，别人的提问也可能把它召回去。
    """
    if not contexts:
        block = "（没有检索到任何相关资料）"
        ordered: list[Context] = []
    else:
        block, ordered = build_context_block(contexts)

    facts = [f for f in (user_facts or []) if str(f).strip()]
    fact_part = f"\n{_user_fact_block(facts)}\n" if facts else ""

    user = f"""参考资料：

{block}

---
{fact_part}
问题：{question}

请依据上述参考资料（以及当前用户档案，如果提供了的话）回答，并标注来源编号。"""
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


def validate_citations(answer: str, sources: list[str]) -> dict:
    """对答案的引用做**确定性**校验（不判断语义是否真的被支持）。

    确定性部分（可硬性判定）：
      - out_of_range：`[n]` 的 n 越界，说明模型编了不存在的来源编号 —— 硬错误
      - total_markers：引用标记总数，为 0 说明整篇没标引用
    启发式部分（仅供参考，不是错误）：
      - uncited_lines：看起来是事实陈述却没标引用的行。过渡句、总结句本就不需要引用，
        所以这里只提示、不判错。真正的「引用是否支持结论」属于 P1 的 faithfulness 范畴。
    """
    markers = [int(m) for m in _CITE_RE.findall(answer)]
    out_of_range = sorted({m for m in markers if m < 1 or m > len(sources)})

    uncited: list[str] = []
    for line in answer.splitlines():
        s = line.strip()
        if len(s) < 20 or _CITE_RE.search(s):
            continue
        if s.startswith(("#", "|", "```", "-", "*", ">")) or s.endswith(("：", ":")):
            continue
        uncited.append(s[:80])

    return {
        "cited": extract_citations(answer, sources),
        "out_of_range": out_of_range,
        "uncited_lines": uncited,
        "total_markers": len(markers),
    }
