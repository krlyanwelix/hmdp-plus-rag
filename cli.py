"""命令行入口。

    python cli.py ingest [--rebuild]     建索引
    python cli.py stats                  看索引状态
    python cli.py search "问题"          只检索，不生成（调检索时最常用）
    python cli.py ask "问题"             完整 RAG 问答
    python cli.py eval [--compare]       跑评测集，--compare 做消融对比
    python cli.py chunks "关键词"        看切块结果（调 chunk_size 时用）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 可选依赖的本地安装目录。用 `pip install --target .pylibs <pkg>` 装在这里，
# 就不必污染共享的 Python 运行时。append 而非 insert，已有的包优先。
_PYLIBS = ROOT / ".pylibs"
if _PYLIBS.is_dir() and str(_PYLIBS) not in sys.path:
    sys.path.append(str(_PYLIBS))

from config import Config  # noqa: E402
from rag.eval.metrics import evaluate, load_dataset  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402


def _pipeline(verbose: bool = True) -> RAGPipeline:
    return RAGPipeline(Config(), verbose=verbose)


# --------------------------------------------------------------------------- #
def cmd_ingest(args) -> int:
    pipe = _pipeline(verbose=getattr(args, "verbose", True))
    stats = pipe.index(rebuild=args.rebuild, raw_dir=args.raw)
    if not stats.get("children"):
        print("索引为空，请检查 data/raw 目录")
        return 1
    return 0


def cmd_stats(args) -> int:
    pipe = _pipeline(verbose=getattr(args, "verbose", True))
    st = pipe.stats()
    width = max(len(k) for k in st)
    for k, v in st.items():
        print(f"  {k:<{width}} : {v}")
    if not st["documents_indexed"]:
        print("\n  索引为空，先执行：python cli.py ingest")
    return 0


def cmd_search(args) -> int:
    pipe = _pipeline(verbose=args.verbose)
    if pipe.is_empty():
        print("索引为空。先执行：python cli.py ingest")
        return 1
    hits = pipe.search(args.question, mode=args.mode, top_k=args.k)
    if not hits:
        print("没有检索到任何结果。")
        return 1

    print(f"\n查询：{args.question}   模式：{args.mode}\n")
    for i, hit in enumerate(hits, start=1):
        head = f"[{i}] score={hit.score:+.4f}  {hit.metadata.get('source', '?')}"
        if hit.metadata.get("heading"):
            head += f" ｜ {hit.metadata['heading']}"
        print(head)
        text = hit.text if args.full else hit.text[:160].replace("\n", " ")
        print(f"    {text}{'' if args.full else '...'}")
        print(f"    chunk_id={hit.chunk_id}")
        print()
    return 0


def cmd_ask(args) -> int:
    pipe = _pipeline(verbose=args.verbose)
    result = pipe.ask(args.question, mode=args.mode)

    if args.show_context or pipe.llm.name == "echo":
        print("\n" + "=" * 68)
        print(f"检索到 {len(result.contexts)} 段上下文（按展示顺序，已做 lost-in-the-middle 重排）")
        print("=" * 68)
        for i, ctx in enumerate(result.contexts, start=1):
            head = f"[{i}] {ctx.source}"
            if ctx.metadata.get("heading"):
                head += f" ｜ {ctx.metadata['heading']}"
            print(f"\n{head}   score={ctx.score:+.4f}")
            body = ctx.text if args.full else ctx.text[:300].replace("\n", " ")
            print(f"    {body}{'' if args.full else '...'}")

    print("\n" + "=" * 68)
    print("答案")
    print("=" * 68)
    print(result.answer)

    dbg = result.debug
    print(
        f"\n[检索统计] 向量 {dbg.get('vector', 0)} 条 / BM25 {dbg.get('bm25', 0)} 条"
        f" -> 融合 {dbg.get('fused', 0)} -> 重排保留 {dbg.get('reranked', 0)}"
        f" -> 上下文 {dbg.get('contexts', 0)} 段"
    )
    if dbg.get("citations"):
        print(f"[引用来源] {', '.join(dbg['citations'])}")
    return 0


def cmd_eval(args) -> int:
    pipe = _pipeline(verbose=args.verbose)
    if pipe.is_empty():
        print("索引为空，评测没有意义。先执行：python cli.py ingest")
        return 1
    dataset = load_dataset(Config().eval_dataset_path)
    print(f"评测集：{len(dataset)} 题，k={args.k}\n")

    modes = ["vector", "bm25", "hybrid"] if args.compare else [args.mode]
    summaries = []
    for mode in modes:
        print(f"--- mode = {mode} ---")
        summaries.append(evaluate(pipe, dataset, k=args.k, mode=mode, verbose=not args.quiet))

    print("\n" + "=" * 76)
    print(
        f"{'mode':<9}{'recall@' + str(args.k):>11}{'证据召回':>11}{'证据@1':>10}"
        f"{'证据MRR':>10}{'来源MRR':>10}{'ndcg':>8}"
    )
    print("-" * 76)
    for s in summaries:
        print(
            f"{s['mode']:<9}{s[f'recall@{args.k}']:>11.3f}{s['证据召回']:>11.3f}"
            f"{s['证据@1']:>10.3f}{s['证据MRR']:>10.3f}{s['mrr']:>10.3f}{s[f'ndcg@{args.k}']:>8.3f}"
        )
    print("=" * 76)

    head = summaries[0]
    print(f"\n共 {head['n']} 题，其中 {head['n_judged']} 题标注了证据要点，"
          f"{head['n_negative']} 题是负样本（用于测生成端幻觉，不计入检索指标）。")
    print("优先看「证据MRR」和「证据@1」：它们对排序敏感，")
    print("来源级 recall 在语料小的时候会饱和到 1.0，没有区分度。")
    print("\n注意：零依赖降级栈（hashing embedder + 词法重排）下三模式分数可能完全相同，")
    print("     因为向量、BM25、重排用的是同一类词法信号，没有互补性。")
    print("     装上 chromadb + sentence-transformers 后差异才会显现。")

    if args.fail_under is not None:
        best = max(s["证据MRR"] for s in summaries)
        if best < args.fail_under:
            print(f"\n[FAIL] 最高证据MRR {best:.3f} < {args.fail_under}")
            return 1
    return 0


def cmd_chunks(args) -> int:
    """展示切块结果，用于人工检查 chunk_size / overlap 是否合适。"""
    from rag.ingest.parse import parse_file
    from rag.ingest.split import split_document

    cfg = Config()
    raw = cfg.raw_path
    files = sorted(p for p in raw.rglob("*") if p.is_file() and not p.name.startswith("."))
    if not files:
        print(f"{raw} 下没有文档")
        return 1

    for f in files:
        doc = parse_file(f, base=raw)
        children, parents = split_document(doc, cfg)
        print(f"\n=== {doc.source}：{len(doc.text)} 字 -> {len(parents)} 父块 / {len(children)} 子块")
        for p in parents[: args.limit]:
            kids = [c for c in children if c.metadata.get("parent_id") == p.chunk_id]
            head = f"  [父 {p.metadata['parent_index']}] {len(p.text)} 字"
            if p.metadata.get("heading"):
                head += f"  ｜ {p.metadata['heading']}"
            print(head)
            for c in kids[: args.limit]:
                snippet = c.text[:70].replace("\n", " ")
                print(f"      └ 子 {c.metadata['chunk_index']}: {len(c.text):>4} 字 | {snippet}...")
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cli.py",
        description="纯 Python RAG 骨架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-q", "--quiet", action="store_true", help="不打印初始化日志")
    # 让 -q 放在子命令之后也能生效；default=SUPPRESS 避免覆盖掉前面那个 -q
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("ingest", help="解析 + 切块 + 向量化 + 建索引", parents=[common])
    s.add_argument("--rebuild", action="store_true", help="清空旧索引后全量重建")
    s.add_argument("--raw", default=None, help="原始文档目录，默认 data/raw")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("stats", help="显示索引与组件状态", parents=[common])
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("search", help="只检索不生成（调检索时用）", parents=[common])
    s.add_argument("question")
    s.add_argument("--mode", choices=["hybrid", "vector", "bm25"], default="hybrid")
    s.add_argument("-k", type=int, default=None, help="返回条数")
    s.add_argument("--full", action="store_true", help="打印完整片段")
    s.set_defaults(func=cmd_search, verbose=False)

    s = sub.add_parser("ask", help="完整 RAG 问答", parents=[common])
    s.add_argument("question")
    s.add_argument("--mode", choices=["hybrid", "vector", "bm25"], default="hybrid")
    s.add_argument("--show-context", action="store_true", help="打印检索到的上下文")
    s.add_argument("--full", action="store_true", help="上下文不截断")
    s.set_defaults(func=cmd_ask, verbose=False)

    s = sub.add_parser("eval", help="跑检索层评测", parents=[common])
    s.add_argument("-k", type=int, default=None, help="评测的 k，默认取配置 EVAL_K")
    s.add_argument("--mode", choices=["hybrid", "vector", "bm25"], default="hybrid")
    s.add_argument("--compare", action="store_true", help="三种模式各跑一遍做消融对比")
    s.add_argument("--fail-under", type=float, default=None, help="recall 低于该值时退出码为 1")
    s.set_defaults(func=cmd_eval, verbose=False)

    s = sub.add_parser("chunks", help="查看切块结果", parents=[common])
    s.add_argument("--limit", type=int, default=3)
    s.set_defaults(func=cmd_chunks, verbose=False)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "verbose"):
        args.verbose = True
    if args.quiet:
        args.verbose = False
    if args.func is cmd_eval and args.k is None:
        args.k = Config().eval_k
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
