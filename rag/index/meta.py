"""索引元信息：记录这份索引是用哪个后端、哪个 embedding 模型建的。

为什么需要这东西：

装了 chromadb 之后 `STORE_BACKEND=auto` 会从 memory 静默切到 chroma，
而新后端的集合是空的 —— 旧索引还躺在磁盘上，检索却返回空结果，
用户只会看到评测变成 0 分，完全不知道发生了什么。

换 embedding 模型同理，后果更隐蔽：新旧向量不在同一个语义空间，
混在一起检索不会报错，只会返回「看起来正常但不对」的结果。

两种情况都必须大声报警，而不是让用户对着 0 分发懵。
"""

from __future__ import annotations

import json
from pathlib import Path

META_FILE = "index_meta.json"


def write_meta(
    store_path: Path | str,
    *,
    backend: str,
    embedder: str,
    embed_model: str = "",
    documents: int = 0,
    children: int = 0,
    parents: int = 0,
) -> None:
    path = Path(store_path)
    path.mkdir(parents=True, exist_ok=True)
    payload = {
        "backend": backend,
        "embedder": embedder,
        "embed_model": embed_model,
        "documents": documents,
        "children": children,
        "parents": parents,
    }
    (path / META_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_meta(store_path: Path | str) -> dict:
    f = Path(store_path) / META_FILE
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def check_meta(
    store_path: Path | str,
    *,
    backend: str,
    embedder: str,
    embed_model: str = "",
) -> list[str]:
    """返回需要提醒用户的问题列表（空列表表示一切正常）。"""
    meta = read_meta(store_path)
    if not meta:
        # 没有元信息说明是旧版本建的索引，或者索引根本不存在，两种情况都不用报错
        return []

    warnings: list[str] = []

    if meta.get("backend") and meta["backend"] != backend:
        warnings.append(
            f"索引是用 {meta['backend']} 后端建的，当前生效的是 {backend}。"
            f"新后端看不到旧数据，检索会返回空结果。请执行：python cli.py ingest --rebuild"
        )

    if meta.get("embedder") and meta["embedder"] != embedder:
        warnings.append(
            f"索引是用 {meta['embedder']} 建的，当前生效的是 {embedder}。"
            f"新旧向量不在同一语义空间，混用会得到错误结果。请执行：python cli.py ingest --rebuild"
        )

    old_model = meta.get("embed_model") or ""
    if old_model and embed_model and old_model != embed_model:
        warnings.append(
            f"索引用的 embedding 模型是 {old_model}，当前配置是 {embed_model}。"
            f"换模型必须重建索引：python cli.py ingest --rebuild"
        )

    return warnings
