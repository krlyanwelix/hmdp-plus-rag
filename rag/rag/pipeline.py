"""在线查询链路编排：retrieve -> (small-to-big) -> prompt -> generate -> cite。"""

from __future__ import annotations

import time

from config import Config
from rag.generate.llm import LLM, get_llm
from rag.generate.prompt import REFUSAL_TEXT, build_messages, validate_citations
from rag.index.embed import get_embedder
from rag.index.meta import check_meta
from rag.index.parents import ParentStore
from rag.index.store import get_store
from rag.indexer import Indexer
from rag.retrieve.rerank import get_reranker
from rag.retrieve.retriever import Retriever
from rag.types import Answer, Context, Hit


def citations_payload(contexts: list[Context]) -> list[dict]:
    """把上下文转成前端能直接渲染的引用卡片。

    编号必须和 prompt 里的 [n] 一致，所以这里用的是 build_messages 返回的
    **展示顺序**（lost-in-middle 重排之后），不是相关性顺序。
    """
    out: list[dict] = []
    for i, ctx in enumerate(contexts, start=1):
        out.append(
            {
                "index": i,
                "source": ctx.source,
                "heading": str(ctx.metadata.get("heading") or ""),
                "score": round(float(ctx.score or 0), 4),
                "eventId": ctx.metadata.get("event_id"),
                "snippet": (ctx.text or "").strip().replace("\n", " ")[:120],
            }
        )
    return out


def fallback_text(contexts: list[Context]) -> str:
    """生成层不可用时的兜底：把检索到的原文按引用编号摊开。

    宁可给用户一段「没有结论但有出处」的原文，也不要给一个 500 或半句话。
    """
    lines = ["（生成服务暂不可用，以下为检索到的规程原文）", ""]
    for i, ctx in enumerate(contexts, start=1):
        head = f"[{i}] {ctx.source}"
        if ctx.metadata.get("heading"):
            head += f" ｜ {ctx.metadata['heading']}"
        lines.append(head)
        lines.append((ctx.text or "").strip()[:400])
        lines.append("")
    return "\n".join(lines).strip()


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
    def search(
        self, question: str, mode: str = "hybrid", top_k: int | None = None, where: dict | None = None
    ) -> list[Hit]:
        """只做检索，不调 LLM。调试检索质量时用这个。"""
        return self.retriever.search(question, mode=mode, top_k=top_k, where=where)

    def _retrieve(
        self,
        question: str,
        mode: str,
        debug: dict,
        where: dict | None,
        source_prefix: str | None,
    ) -> list[Context]:
        contexts = self.retriever.retrieve(
            question, mode=mode, debug=debug, where=where, source_prefix=source_prefix
        )
        # 老索引没有 event_id metadata 时 where 会全空，退回前缀过滤，保证服务不瘫
        if not contexts and where and source_prefix:
            contexts = self.retriever.retrieve(
                question, mode=mode, debug=debug, source_prefix=source_prefix
            )
        return contexts

    def ask(
        self,
        question: str,
        mode: str = "hybrid",
        where: dict | None = None,
        source_prefix: str | None = None,
        user_facts: list[str] | None = None,
    ) -> Answer:
        debug: dict = {}
        contexts = self._retrieve(question, mode, debug, where, source_prefix)
        if not contexts:
            # 没有可用上下文就别浪费一次生成调用：确定性拒答比让模型"自由发挥"安全
            return Answer(
                question=question,
                answer=REFUSAL_TEXT,
                contexts=[],
                debug={"contexts": 0, "refused": True},
            )
        messages, ordered = build_messages(question, contexts, user_facts=user_facts)

        # EchoLLM 会从这里读取上下文，所以必须在 chat 之前赋值
        self._last_contexts = ordered
        try:
            answer = self.llm.chat(messages)
        except Exception as exc:
            # 生成层挂了不能连累检索结果：退回原文片段，至少还有出处
            answer = fallback_text(ordered)
            debug["llm_error"] = f"{type(exc).__name__}: {exc}"

        check = validate_citations(answer, [c.source for c in ordered])
        debug["citation_check"] = check
        debug["citations"] = check["cited"]
        debug["usage"] = dict(getattr(self.llm, "last_usage", {}) or {})
        return Answer(question=question, answer=answer, contexts=ordered, debug=debug)

    def stream(
        self,
        question: str,
        mode: str = "hybrid",
        where: dict | None = None,
        source_prefix: str | None = None,
        user_facts: list[str] | None = None,
    ):
        """流式问答，yield (事件名, 数据字典)。

        meta  检索完成、开始生成：此时前端就能渲染引用卡片
        delta 增量文本
        done  结束：带完整答案与引用校验结果
        error 生成中断（检索阶段的异常由调用方处理，因为在第一个 yield 之前）
        """
        debug: dict = {}
        contexts = self._retrieve(question, mode, debug, where, source_prefix)
        if not contexts:
            yield "meta", {"citations": [], "contexts": 0}
            yield "delta", {"text": REFUSAL_TEXT}
            yield "done", {
                "answer": REFUSAL_TEXT,
                "citations": [],
                "cited": [],
                "outOfRange": [],
                "usage": {},
                "refused": True,
            }
            return
        messages, ordered = build_messages(question, contexts, user_facts=user_facts)
        self._last_contexts = ordered

        yield "meta", {
            "citations": citations_payload(ordered),
            "contexts": len(ordered),
            "retrieval": {k: debug.get(k) for k in ("vector", "bm25", "fused", "reranked")},
        }

        pieces: list[str] = []
        buffer: list[str] = []
        emitted = False
        last_flush = time.monotonic()
        # 墙钟上限：socket 超时只能兜住「一个字节都不来」，兜不住「每秒来一个字」。
        # 上游慢速滴灌时，只有这个 deadline 能把连接主动断掉，不让 SSE 挂到天亮。
        budget = float(getattr(self.cfg, "llm_deadline", 0) or 0)
        deadline = (last_flush + budget) if budget > 0 else None
        try:
            for piece in self.llm.chat_stream(messages):
                if deadline is not None and time.monotonic() > deadline:
                    raise TimeoutError(f"生成超过 {budget:.0f}s 上限，已主动中断")
                pieces.append(piece)
                buffer.append(piece)
                text = "".join(buffer)
                now = time.monotonic()
                # 第一段立刻发（首字延迟优先），之后按"12 字或 60ms"合批：
                # deepseek 的 delta 粒度是 1~2 个字，不合批会退化成几百个 SSE 事件，
                # 每条都要走一次 JSON 序列化 + 一次 socket flush，得不偿失。
                if not emitted or len(text) >= 12 or (now - last_flush) >= 0.06:
                    yield "delta", {"text": text}
                    emitted = True
                    buffer.clear()
                    last_flush = now
            if buffer:
                yield "delta", {"text": "".join(buffer)}
        except Exception as exc:
            # 已经吐出去的内容保留，再补一段原文兜底，别让用户看到半句话
            yield "error", {"message": f"生成中断：{str(exc)[:200]}"}
            partial = "".join(pieces)
            fallback = (partial + "\n\n" + fallback_text(ordered)) if partial else fallback_text(ordered)
            yield "done", {
                "answer": fallback,
                "citations": citations_payload(ordered),
                "cited": [],
                "outOfRange": [],
                "usage": {},
                "llmError": str(exc)[:200],
            }
            return

        answer = "".join(pieces)
        check = validate_citations(answer, [c.source for c in ordered])
        yield "done", {
            "answer": answer,
            "citations": citations_payload(ordered),
            "cited": check["cited"],
            "outOfRange": check["out_of_range"],
            "usage": dict(getattr(self.llm, "last_usage", {}) or {}),
        }

    # ---------------- 状态 ----------------
    def stats(self) -> dict:
        return {
            "documents_indexed": self.store.count(),
            "parents": self.parents.count(),
            "embedder": self.embedder.name,
            "reranker": self.reranker.name,
            "device": getattr(self.embedder, "device", None) or getattr(self.reranker, "device", None) or "cpu",
            "store": self.store.backend,
            "llm": self.llm.name,
            "bm25_enabled": self.cfg.use_bm25,
            # milvus 后端连的是服务地址，chroma/memory 才是本地目录，分开报才不会被误读
            "store_path": (
                self.cfg.milvus_uri
                if self.store.backend == "milvus"
                else str(self.cfg.store_path)
            ),
        }
