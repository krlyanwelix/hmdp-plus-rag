"""离线索引链路编排：parse -> split -> embed -> store。

支持增量：默认只覆盖「本次扫到的文档」，按 source 先删后加（幂等）。
这样加一篇文档不必重建全库；但改了 embedding 模型就必须 --rebuild，
因为新旧向量不在同一语义空间，混在一起检索会失真。
"""

from __future__ import annotations

import re
from pathlib import Path

from config import Config
from rag.index.embed import Embedder, get_embedder
from rag.index.meta import write_meta
from rag.index.parents import ParentStore
from rag.index.store import VectorStore, get_store
from rag.ingest.parse import load_dir
from rag.ingest.split import split_documents

EMBED_BATCH = 64
_EVENT_RE = re.compile(r"^e(\d+)[-_]", re.IGNORECASE)


def event_id_of(source: str) -> int | None:
    """从文件名前缀解析赛事 id，如 "E1-竞赛规程.md" -> 1。

    重建索引时把 event_id 写进 metadata，检索就能用 where 在**向量库层面**过滤，
    而不是先取回 top_k 再丢一半。文件名是唯一可靠的来源标识（parse 层不改文档内容）。
    """
    m = _EVENT_RE.match(Path(source).name)
    return int(m.group(1)) if m else None


class Indexer:
    def __init__(
        self,
        cfg: Config,
        embedder: Embedder | None = None,
        store: VectorStore | None = None,
        parents: ParentStore | None = None,
        verbose: bool = True,
    ):
        self.cfg = cfg
        self.verbose = verbose
        self.embedder = embedder or get_embedder(cfg, verbose=verbose)
        self.store = store or get_store(cfg, verbose=verbose)
        self.parents = parents or ParentStore(cfg.store_path)

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg)

    def build(self, rebuild: bool = False, raw_dir: Path | str | None = None) -> dict:
        raw = Path(raw_dir) if raw_dir else self.cfg.raw_path
        self._log(f"[1/4] 解析文档：{raw}")
        docs = load_dir(raw)
        if not docs:
            self._log("  没有找到可索引的文档")
            return {"documents": 0, "children": 0, "parents": 0}

        self._log(f"  解析到 {len(docs)} 篇文档")
        for d in docs[:5]:
            self._log(f"    - {d.source}（{len(d.text)} 字）")
        if len(docs) > 5:
            self._log(f"    ... 其余 {len(docs) - 5} 篇")

        self._log("[2/4] 切块（父块 + 子块）")
        children, parents = split_documents(docs, self.cfg)
        self._log(
            f"  子块 {len(children)} 个（检索单元，目标 {self.cfg.chunk_size} 字）"
            f"，父块 {len(parents)} 个（上下文单元，目标 {self.cfg.parent_size} 字）"
        )
        if not children:
            return {"documents": len(docs), "children": 0, "parents": len(parents)}

        self._log("[3/4] 向量化")
        if rebuild:
            self.store.reset()
            self.parents.reset()
            self._log("  已清空旧索引（--rebuild）")
        else:
            for doc in docs:
                self.store.delete_by_source(doc.source)
                self.parents.delete_by_source(doc.source)

        records = []
        total = len(children)
        for start in range(0, total, EMBED_BATCH):
            batch = children[start : start + EMBED_BATCH]
            vecs = self.embedder.encode([c.text for c in batch], is_query=False)
            for chunk, vec in zip(batch, vecs):
                event_id = event_id_of(chunk.source)
                records.append(
                    {
                        "id": chunk.chunk_id,
                        "text": chunk.text,
                        "embedding": vec,
                        "metadata": {
                            "source": chunk.source,
                            "doc_id": chunk.doc_id,
                            **({"event_id": event_id} if event_id is not None else {}),
                            **chunk.metadata,
                        },
                    }
                )
            self._log(f"  {min(start + EMBED_BATCH, total)}/{total}")

        self._log("[4/4] 写入索引")
        self.store.add(records)
        self.parents.add(parents)
        self.store.persist()
        self.parents.persist()

        stats = {
            "documents": len(docs),
            "children": len(children),
            "parents": len(parents),
            "embedder": self.embedder.name,
            "store": self.store.backend,
            "store_total": self.store.count(),
        }

        # 记下这份索引用什么建的，下次若后端或模型变了就能立刻报警
        write_meta(
            self.cfg.store_path,
            backend=self.store.backend,
            embedder=self.embedder.name,
            embed_model=getattr(self.embedder, "model_name", ""),
            documents=stats["documents"],
            children=stats["children"],
            parents=stats["parents"],
        )

        # 索引变了，跨实例缓存必须整体失效：版本号一升，旧 key 自然再也查不到，
        # 不用去扫/删成千上万个 key（Redis 里没有按前缀批量删的低成本方案）。
        try:
            from rag.cache import AnswerCache

            AnswerCache.from_env().bump_version()
        except Exception as exc:  # 缓存不是正确性依赖，失败只记日志
            self._log(f"  [cache] 版本号更新失败（不影响检索）：{exc}")

        self._log(
            f"  完成：向量库 {stats['store_total']} 条（{stats['store']}）"
            f"，父块 {self.parents.count()} 个；embedder={stats['embedder']}"
        )
        return stats
