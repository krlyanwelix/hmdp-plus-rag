"""交互式 RAG 问答。

加载一次模型（embedding + reranker + LLM），然后循环提问，避免每次冷启动 40s+。

用法：
    python scripts/chat.py

输入问题回车即问。特殊命令：
    /ctx        显示上一次检索到的上下文（含来源、章节、得分）
    quit / q    退出
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.append(os.path.join(_ROOT, ".pylibs"))

# 尽量保证中文输出不因终端编码乱码（PowerShell 里建议再配合下面的设置）
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from config import Config  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402


def main() -> None:
    print("正在加载模型（bge-m3 + bge-reranker-v2-m3 + LLM），首次约 40-60 秒…")
    pipe = RAGPipeline(Config())

    st = pipe.stats()
    print("\n加载完成。当前组件：")
    for k, v in st.items():
        print(f"  {k:<18} : {v}")

    print("\n" + "=" * 60)
    print("开始提问吧。输入 quit / q 退出，输入 /ctx 看检索上下文")
    print("=" * 60)

    show_ctx = False
    while True:
        try:
            q = input("\n问题 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break

        if not q:
            continue
        if q.lower() in ("quit", "exit", "q", "退出"):
            print("再见。")
            break
        if q in ("/ctx", "/c"):
            show_ctx = not show_ctx
            print("显示检索上下文：", "开" if show_ctx else "关")
            continue

        result = pipe.ask(q)

        if show_ctx:
            print("\n" + "-" * 60)
            print(f"检索到 {len(result.contexts)} 段上下文（按展示顺序）：")
            for i, ctx in enumerate(result.contexts, start=1):
                head = f"[{i}] {ctx.source}"
                if ctx.metadata.get("heading"):
                    head += f" ｜ {ctx.metadata['heading']}"
                print(f"  {head}  (score={ctx.score:+.4f})")
            print("-" * 60)

        print("\n答案：")
        print(result.answer)

        dbg = result.debug
        cited = list(dict.fromkeys(dbg.get("citations", [])))
        print("\n[引用来源] " + (", ".join(cited) if cited else "无"))
        print(
            f"[检索] 向量 {dbg.get('vector', 0)} 条 / BM25 {dbg.get('bm25', 0)} 条"
            f" -> 融合 {dbg.get('fused', 0)} -> 重排保留 {dbg.get('reranked', 0)}"
            f" -> 上下文 {dbg.get('contexts', 0)} 段"
        )


if __name__ == "__main__":
    main()
