"""父块存储（small-to-big 的另一半）。

父块只按 id 取回，从不参与相似度计算，所以没必要放进向量库浪费向量维度和检索时间。
单独用 JSON 存放即可，逻辑也更清楚。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rag.types import Chunk, Context


class ParentStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._file = self.path / "parents.json"
        self._items: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            self._items = json.loads(self._file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[parents] 读取失败，已忽略：{exc}")
            self._items = {}

    def persist(self) -> None:
        self._file.write_text(
            json.dumps(self._items, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def add(self, chunks: list[Chunk]) -> None:
        for c in chunks:
            self._items[c.chunk_id] = {
                "text": c.text,
                "source": c.source,
                "doc_id": c.doc_id,
                "metadata": c.metadata,
            }

    def get(self, parent_id: str) -> dict[str, Any] | None:
        return self._items.get(parent_id)

    def get_many(self, parent_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {pid: self._items[pid] for pid in parent_ids if pid in self._items}

    def to_contexts(self, parent_ids: list[str], scores: dict[str, float]) -> list[Context]:
        """按给定顺序把父块展开成最终上下文，保持 scores 里记录的最佳子块得分。"""
        out: list[Context] = []
        for pid in parent_ids:
            item = self._items.get(pid)
            if not item:
                continue
            out.append(
                Context(
                    text=item["text"],
                    source=item.get("source", ""),
                    score=float(scores.get(pid, 0.0)),
                    metadata={**item.get("metadata", {}), "parent_id": pid},
                )
            )
        return out

    def delete_by_source(self, source: str) -> int:
        victims = [k for k, v in self._items.items() if v.get("source") == source]
        for k in victims:
            del self._items[k]
        return len(victims)

    def reset(self) -> None:
        self._items = {}
        if self._file.exists():
            self._file.unlink()

    def count(self) -> int:
        return len(self._items)
