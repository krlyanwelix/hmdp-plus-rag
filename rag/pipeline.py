"""在线查询链路编排：retrieve -> (small-to-big) -> prompt -> generate -> cite。"""

from __future__ import annotations

from config import Config
from rag.generate.llm import LLM, get_llm
from rag.generate.prompt import build_messages, extract_citations
from rag.index.embed import get_embedder
from rag.index.meta import check_meta
from rag.index.parents import ParentStore
from rag.index.store import get_store
from rag.indexer import Indexer
from rag.retrieve.rerank import get_reranker
from rag.retrieve.retriever import Retriever
from rag.types import Answer, Context, Hit


class RAGPipeline:
    """把两条链路串起来的门面。CLI 和评测都只用这个类。"""

    def __init__(self, cfg: Config | None = None, verbose: bool = True):
        self.cfg = cfg or Config()
        self.verbose = verbose

        self.embedder = get_embedder(self.cfg, verbose=verbose)
        self.store = get_store(self.cfg, verbose=verbose)
        self.parents = ParentStore(self.cfg.store_path)
        self.reranker = get_reranker(self.cfg, verbose=verbose)
        self.retriever = Retriever(
            self.cfg,
            embedder=self.embedder,
            store=self.store,
            parents=self.parents,
            reranker=self.reranker,
            verbose=verbose,
        )

        self._last_contexts: list[Context] = []
        self.llm: LLM = get_llm(
            self.cfg,
            contexts_provider=lambda: self._last_contexts,
            verbose=verbose,
        )

        self._warn_index_mismatch()

    # ---------------- 索引健康检查 ----------------
    def _warn_index_mismatch(self) -> None:
        """索引与当前配置不匹配时大声报警。

        最容易踩的一种情况：装了 chromadb 之后 STORE_BACKEND=auto 会切到 chroma，
        而 chroma 的集合是空的，旧索引还在但检索返回空 —— 不提示的话只能看到评测 0 分。

        注意这里**不受 verbose 控制**：这是正确性问题，不是日志噪音，
        而且 search / eval 默认就是 verbose=False，恰恰是最需要看到它的时候。
        """
        problems = check_meta(
            self.cfg.store_path,
            backend=self.store.backend,
            embedder=self.embedder.name,
            embed_model=getattr(self.embedder, "model_name", ""),
        )
        for p in problems:
            print(f"[!] {p}")

    def is_empty(self) -> bool:
        return self.store.count() == 0

    # ---------------- 离线 ----------------
    def index(self, rebuild: bool = False, raw_dir=None) -> dict:
        stats = Indexer(
            self.cfg,
            embedder=self.embedder,
            store=self.store,
            parents=self.parents,
            verbose=self.verbose,
        ).build(rebuild=rebuild, raw_dir=raw_dir)
        self.retriever.invalidate()  # 索引变了，BM25 必须重建
        return stats

    # ---------------- 在线 ----------------
    def search(self, question: str, mode: str = "hybrid", top_k: int | None = None) -> list[Hit]:
        """只做检索，不调 LLM。调试检索质量时用这个。"""
        return self.retriever.search(question, mode=mode, top_k=top_k)

    def ask(self, question: str, mode: str = "hybrid") -> Answer:
        debug: dict = {}
        contexts = self.retriever.retrieve(question, mode=mode, debug=debug)
        messages, ordered = build_messages(question, contexts)

        # EchoLLM 会从这里读取上下文，所以必须在 chat 之前赋值
        self._last_contexts = ordered
        answer = self.llm.chat(messages)

        debug["citations"] = extract_citations(answer, [c.source for c in ordered])
        return Answer(question=question, answer=answer, contexts=ordered, debug=debug)

    # ---------------- 状态 ----------------
    def stats(self) -> dict:
        return {
            "documents_indexed": self.store.count(),
            "parents": self.parents.count(),
            "embedder": self.embedder.name,
            "reranker": self.reranker.name,
            "store": self.store.backend,
            "llm": self.llm.name,
            "bm25_enabled": self.cfg.use_bm25,
            "store_path": str(self.cfg.store_path),
        }
