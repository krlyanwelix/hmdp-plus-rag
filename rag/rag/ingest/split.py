"""切块：递归字符切分 + 父子块（small-to-big）。

为什么必须切：embedding 是有损压缩，把长文本压成一个定长向量。
文本越长、主题越多，向量越接近「平均语义」，无法表达任何一个具体点。
所以 chunk 是检索的最小语义单元，它的质量直接决定召回上限。

核心矛盾：检索要小块（精准），生成要大块（完整）。
解法就是父子块——子块进索引，父块进 prompt。

本模块的切分函数会一并返回每个块的**字符偏移**，
这样才能给每个子块标注它真正所属的章节，而不是继承父块的章节。
"""

from __future__ import annotations

from rag.types import Chunk, Document

# 按「语义边界强度」从强到弱排列，递归时优先在强边界切。
DEFAULT_SEPARATORS = [
    "\n\n",   # 段落
    "\n",     # 行
    "。", "！", "？", "；",   # 中文句末
    ". ", "! ", "? ", "; ",   # 英文句末（带空格，避免切碎小数和缩写）
    "，", ", ",               # 从句
    " ", "",                  # 兜底：词边界 -> 硬切
]


def _split_keep_sep(text: str, sep: str) -> list[str]:
    """按 sep 切分，但把分隔符保留在前一段末尾，避免语义被削掉。"""
    if not sep:
        return [text]
    raw = text.split(sep)
    out: list[str] = []
    for i, piece in enumerate(raw):
        if i < len(raw) - 1:
            out.append(piece + sep)
        elif piece:
            out.append(piece)
    return [p for p in out if p]


def _split_recursive(text: str, size: int, seps: list[str], base: int = 0) -> list[tuple[str, int]]:
    """返回 [(文本, 在本次调用输入中的起始偏移), ...]，保证每块 <= size。"""
    if not text:
        return []
    if len(text) <= size:
        return [(text, base)]
    if not seps:
        return [(text[i : i + size], base + i) for i in range(0, len(text), size)]

    sep, rest = seps[0], seps[1:]
    if not sep or sep not in text:
        return _split_recursive(text, size, rest, base)

    parts = _split_keep_sep(text, sep)
    offsets: list[int] = []
    cursor = 0
    for part in parts:
        offsets.append(cursor)
        cursor += len(part)

    out: list[tuple[str, int]] = []
    buf, buf_off = "", base
    for part, poff in zip(parts, offsets):
        if len(part) > size:
            # 单段就超长：先冲掉缓冲区，再对这一段用更弱的边界继续切
            if buf.strip():
                out.append((buf, buf_off))
            buf = ""
            out.extend(_split_recursive(part, size, rest, base + poff))
            continue
        if not buf:
            buf, buf_off = part, base + poff
        elif len(buf) + len(part) <= size:
            buf += part
        else:
            if buf.strip():
                out.append((buf, buf_off))
            buf, buf_off = part, base + poff
    if buf.strip():
        out.append((buf, buf_off))
    return out


def recursive_split_with_offsets(
    text: str,
    size: int,
    overlap: int = 0,
    separators: list[str] | None = None,
) -> list[tuple[str, int]]:
    """切分并返回每块的起始偏移。

    注意：offset 指向块的**正文起点**，不包含 overlap 拼进来的前文尾巴。
    """
    if not text or not text.strip():
        return []
    seps = list(DEFAULT_SEPARATORS if separators is None else separators)

    items: list[tuple[str, int]] = []
    for piece, off in _split_recursive(text, size, seps):
        lead = len(piece) - len(piece.lstrip())
        stripped = piece.strip()
        if stripped:
            items.append((stripped, off + lead))

    if overlap > 0 and len(items) > 1:
        merged = [items[0]]
        for (prev_text, _), (cur_text, cur_off) in zip(items, items[1:]):
            merged.append((prev_text[-overlap:] + cur_text, cur_off))
        items = merged
    return items


def recursive_split(
    text: str,
    size: int,
    overlap: int = 0,
    separators: list[str] | None = None,
) -> list[str]:
    """只要文本，不要偏移。"""
    return [t for t, _ in recursive_split_with_offsets(text, size, overlap, separators)]


def split_document(doc: Document, cfg) -> tuple[list[Chunk], list[Chunk]]:
    """把一篇文档切成 (子块, 父块)。

    父块不重叠（它只是上下文容器），子块重叠（它要让检索不断裂）。
    """
    child_chunks: list[Chunk] = []
    parent_chunks: list[Chunk] = []

    for pi, (ptext, poff) in enumerate(recursive_split_with_offsets(doc.text, cfg.parent_size, 0)):
        parent_id = f"{doc.doc_id}::p{pi}"
        parent_heading = doc.label_at(poff)

        parent_chunks.append(
            Chunk(
                chunk_id=parent_id,
                doc_id=doc.doc_id,
                source=doc.source,
                text=ptext,
                kind="parent",
                metadata={"parent_index": pi, "heading": parent_heading},
            )
        )

        # 过短的尾块并入前一块，避免产生无意义的碎片
        merged: list[tuple[str, int]] = []
        for ktext, koff in recursive_split_with_offsets(ptext, cfg.chunk_size, cfg.chunk_overlap):
            if merged and len(ktext) < cfg.min_chunk_size:
                merged[-1] = (merged[-1][0] + "\n" + ktext, merged[-1][1])
            else:
                merged.append((ktext, koff))

        for ci, (ctext, coff) in enumerate(merged):
            child_chunks.append(
                Chunk(
                    chunk_id=f"{parent_id}::c{ci}",
                    doc_id=doc.doc_id,
                    source=doc.source,
                    text=ctext,
                    kind="child",
                    metadata={
                        "parent_id": parent_id,
                        "chunk_index": ci,
                        # 用子块自己的偏移定位章节，而不是继承父块的
                        "heading": doc.label_at(poff + coff),
                        "offset": poff + coff,
                    },
                )
            )

    return child_chunks, parent_chunks


def split_documents(docs: list[Document], cfg) -> tuple[list[Chunk], list[Chunk]]:
    children: list[Chunk] = []
    parents: list[Chunk] = []
    for doc in docs:
        c, p = split_document(doc, cfg)
        children.extend(c)
        parents.extend(p)
    return children, parents
