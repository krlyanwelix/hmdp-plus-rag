"""P0 验收回归：自动验证「接通真实生成链路」的三条标准。

用法： python scripts/verify_p0.py

逐条验收（对应 ROADMAP 的 P0）：
  1) 正常问答：答案必须带 [n] 引用，且编号不越界（不能编造来源）
  2) 负样本：问文档中不存在的型号，答案必须拒绝回答而非编造
  3) 成本：记录一次完整问答的 token 用量

退出码 0 = 全部通过；1 = 有失败项。可挂进 CI 做回归。

注意：会真实调用 LLM，产生少量 token 消耗。
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.append(os.path.join(_ROOT, ".pylibs"))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from config import Config  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402

# 正常问答：覆盖「精确术语」「跨文档」「单文档事实」三类
NORMAL_CASES = [
    "XR-2000 的最大并发连接数是多少？",
    "E1042 是什么错误，怎么排查？",
    "部署 XR-2000 需要哪个版本的 Redis？",
]

# 负样本：文档中不存在的型号，模型必须承认不知道
NEGATIVE_CASES = [
    "XT-9000 支持多少并发连接？",
    "XR-2000 在火星环境下的散热表现如何？",
]

# 拒绝回答的表述（任一命中即视为正确拒绝）
REFUSAL_MARKERS = [
    "无法回答", "无法确定", "没有", "不包含", "未提及", "不存在",
    "缺乏", "缺少", "无法给出", "没有相关", "未找到", "不能回答",
]


def check_answer(ans: str) -> tuple[bool, str]:
    """负样本判定：必须出现拒绝表述。"""
    hit = [m for m in REFUSAL_MARKERS if m in ans]
    return bool(hit), (f"命中拒绝词 {hit[:3]}" if hit else "未出现任何拒绝表述")


def main() -> int:
    cfg = Config()
    print("=" * 68)
    print("P0 验收回归 —— 真实生成链路")
    print("=" * 68)
    print(f"LLM      : {cfg.llm_model} @ {cfg.llm_base_url}")
    print(f"embedder : {cfg.embedder}")
    print(f"reranker : {cfg.reranker}")
    print()

    print("加载 pipeline（全 API 架构，不加载本地模型）…", flush=True)
    t0 = time.time()
    pipe = RAGPipeline(cfg, verbose=False)
    if pipe.is_empty():
        print("索引为空，先执行：python cli.py ingest")
        return 1
    print(f"就绪，耗时 {time.time() - t0:.1f}s\n")

    results: list[tuple[str, str, bool, str]] = []
    tokens_in = tokens_out = 0

    # ---- 验收 1：正常问答的引用有效性 ----
    print("-" * 68)
    print("验收 1：正常问答必须带有效引用（不越界）")
    print("-" * 68)
    for q in NORMAL_CASES:
        r = pipe.ask(q)
        chk = r.debug.get("citation_check") or {}
        usage = r.debug.get("usage") or {}
        tokens_in += usage.get("prompt_tokens", 0)
        tokens_out += usage.get("completion_tokens", 0)

        marks = chk.get("total_markers", 0)
        oob = chk.get("out_of_range") or []
        ok = marks > 0 and not oob

        detail = f"引用标记 {marks} 处，覆盖 {len(chk.get('cited') or [])} 个来源"
        if oob:
            detail += f"；越界编号 {oob}"
        results.append(("引用有效性", q, ok, detail))
        print(f"  [{'OK ' if ok else 'FAIL'}] {q}")
        print(f"         {detail}")

    # ---- 验收 2：负样本不幻觉 ----
    print()
    print("-" * 68)
    print("验收 2：负样本必须拒绝回答，不得编造")
    print("-" * 68)
    for q in NEGATIVE_CASES:
        r = pipe.ask(q)
        usage = r.debug.get("usage") or {}
        tokens_in += usage.get("prompt_tokens", 0)
        tokens_out += usage.get("completion_tokens", 0)

        ok, why = check_answer(r.answer)
        results.append(("负样本不幻觉", q, ok, why))
        print(f"  [{'OK ' if ok else 'FAIL'}] {q}")
        print(f"         {why}")
        print(f"         答案首句：{r.answer.strip().splitlines()[0][:70]}")

    # ---- 验收 3：token 成本已记录 ----
    print()
    print("-" * 68)
    print("验收 3：token 用量已记录")
    print("-" * 68)
    total = tokens_in + tokens_out
    ok = total > 0
    results.append(("token 记录", "usage 非空", ok, f"输入 {tokens_in} + 输出 {tokens_out}"))
    print(f"  [{'OK ' if ok else 'FAIL'}] 输入 {tokens_in} token，输出 {tokens_out} token，"
          f"合计 {total}")
    if cfg.llm_price_input or cfg.llm_price_output:
        est = tokens_in / 1e6 * cfg.llm_price_input + tokens_out / 1e6 * cfg.llm_price_output
        print(f"         估算成本：{est:.6f}（单价 输入 {cfg.llm_price_input}/M、"
              f"输出 {cfg.llm_price_output}/M）")
    else:
        print("         （未配置 LLM_PRICE_INPUT/OUTPUT，跳过成本估算）")

    # ---- 汇总 ----
    passed = sum(1 for r in results if r[2])
    total_cases = len(results)
    print()
    print("=" * 68)
    print(f"结果：{passed}/{total_cases} 通过")
    fails = [r for r in results if not r[2]]
    for kind, q, _, detail in fails:
        print(f"  FAIL [{kind}] {q} —— {detail}")
    print("=" * 68)
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
