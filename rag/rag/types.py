"""贯穿全流程的数据结构。

设计原则：每一层只依赖这里定义的类型，不互相依赖实现，方便单独替换。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Document:
    """一篇解析完成的原始文档。"""

    doc_id: str
    source: str  # 相对路径，用于引用溯源
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    # 结构锚点：[(字符偏移, 标签)]，标签形如 markdown 标题或 "第3页"。
    # 切块时用于给每个 chunk 附加所属章节，检索结果可显示来源位置。
    anchors: list[tuple[int, str]] = field(default_factory=list)

    def label_at(self, offset: int) -> str:
        """返回 offset 处生效的最近一个锚点标签。"""
        label = ""
        for pos, name in self.anchors:
            if pos <= offset:
                label = name
            else:
                break
        return label


@dataclass
class Chunk:
    """切块产物。

    kind="child"  参与向量检索与 BM25 检索，追求「找得准」。
    kind="parent" 不参与检索，只在命中后取回喂给 LLM，追求「上下文完整」。
    """

    chunk_id: str
    doc_id: str
    source: str
    text: str
    kind: str = "child"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hit:
    """检索命中的原始结果（尚未做 small-to-big 展开）。"""

    chunk_id: str
    score: float
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Context:
    """最终交给 LLM 的上下文片段（已完成 small-to-big 展开与重排）。"""

    text: str
    source: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Answer:
    question: str
    answer: str
    contexts: list[Context]
    debug: dict[str, Any] = field(default_factory=dict)
