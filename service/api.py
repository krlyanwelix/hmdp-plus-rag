"""赛事助手 RAG 服务（HTTP 层）。

把纯 CLI 的 RAG 骨架包成 Spring Boot 能调用的服务，职责边界：

1. **预热**：启动时把 embedder / vector store / reranker 全部实例化，
   避免第一个请求承担模型冷启动（CLI 模式下这笔开销每次 ask 都要付）。
2. **赛事隔离**：`eventId` 下沉成向量库的 metadata `where={"event_id": n}`，
   过滤发生在 ANN 阶段，top_k 名额不会被别的赛事占满。
3. **用户隔离**：userId 来自 Java 侧的 HMAC 签名头，**不读请求体**；
   用户私有数据（我的报名）由 Java 查库后以 userFacts 注入，不进向量库 ——
   一旦进库，别人的提问也可能把它召回去。
4. **跨实例缓存**：Redis + 全局版本号；Redis 不可用时自动退回进程内 LRU。
5. **明确拒答 + 降级**：检索不到就直说未收录；生成层失败退回原文片段；
   任何异常都变成可展示的结果，不把 5xx 甩给报名主链路。

启动：
    E:\\Python312\\python.exe service\\run.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 先加 .pylibs：工作区自带的依赖目录（cli.py 也是这么做的）
sys.path.insert(0, str(ROOT / ".pylibs"))
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from config import Config  # noqa: E402
from rag.cache import AnswerCache  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.ratelimit import RateLimiter  # noqa: E402
from service.security import configured_secret, verify  # noqa: E402

APP_NAME = "yejing-rag"
MAX_QUESTION = 500
MAX_USER_FACTS = 8
MAX_FACT_LEN = 200
DEFAULT_TOP_K = 5

app = FastAPI(title="赛事助手 RAG", version="1.1.0")

_pipeline: RAGPipeline | None = None
_pipeline_error: str | None = None
_pipeline_lock = threading.Lock()

_cache: AnswerCache | None = None
_cache_lock = threading.Lock()

_limiter: RateLimiter | None = None
_limiter_lock = threading.Lock()


class AskRequest(BaseModel):
    question: str = ""
    eventId: int | None = None
    topK: int = DEFAULT_TOP_K
    useCache: bool = True
    mode: str = "hybrid"
    userFacts: list[str] | None = None


def get_pipeline() -> RAGPipeline:
    global _pipeline, _pipeline_error
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                t0 = time.perf_counter()
                try:
                    _pipeline = RAGPipeline(Config(), verbose=True)
                    _pipeline_error = None
                    print(
                        f"[api] pipeline 预热完成，耗时 {time.perf_counter() - t0:.2f}s",
                        flush=True,
                    )
                except Exception as exc:  # 记录原因，让 /healthz 能直接暴露
                    _pipeline_error = f"{type(exc).__name__}: {exc}"
                    print(f"[api] pipeline 预热失败：{_pipeline_error}", flush=True)
                    raise
    return _pipeline


def get_cache() -> AnswerCache:
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = AnswerCache.from_env()
                print(f"[api] 缓存初始化：{_cache.stats()}", flush=True)
    return _cache


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                cfg = Config()
                _limiter = RateLimiter(
                    per_min=cfg.rate_limit_per_min,
                    burst=cfg.rate_limit_burst,
                    enabled=cfg.rate_limit_enabled,
                )
                print(f"[api] 限流初始化：{_limiter.stats()}", flush=True)
    return _limiter


@app.on_event("startup")
def _warmup() -> None:
    try:
        get_pipeline()
        get_cache()
    except Exception:
        # 预热失败不阻止进程启动：/healthz 会报错，上游据此降级
        pass


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# 模型被要求"资料不足就说不知道"，但它是自然语言表达，不会只输出空串。
# 用窄口径标记把"软拒答"标出来，前端才能统一走「没收录」的展示。
_REFUSAL_MARKERS = (
    "无法回答",
    "未收录",
    "未找到相关",
    "资料中没有",
    "没有相关信息",
    "无法确定",
    "资料不足",
    "未提及",
    "没有检索到",
    "无法查询",
)


def _looks_refused(answer: str) -> bool:
    text = (answer or "").strip()
    if not text:
        return True
    return any(marker in text[:100] for marker in _REFUSAL_MARKERS)


def _normalize(req: AskRequest) -> AskRequest:
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question 不能为空")
    if len(question) > MAX_QUESTION:
        raise HTTPException(status_code=400, detail=f"question 超过 {MAX_QUESTION} 字")

    facts: list[str] = []
    for fact in (req.userFacts or [])[:MAX_USER_FACTS]:
        text = str(fact).strip()
        if text:
            facts.append(text[:MAX_FACT_LEN])

    top_k = req.topK if isinstance(req.topK, int) and 1 <= req.topK <= 10 else DEFAULT_TOP_K
    return AskRequest(
        question=question,
        eventId=req.eventId,
        topK=top_k,
        useCache=req.useCache,
        mode=req.mode if req.mode in {"hybrid", "vector", "bm25"} else "hybrid",
        userFacts=facts,
    )


def _authorize(raw: bytes, user: str | None, ts: str | None, signature: str | None) -> int | None:
    """验签并解出 userId。验不过直接 401，不做任何检索。

    这是「用户维度隔离」的第一道闸：userId 只能由持有共享密钥的 Java 侧给出。
    """
    ok, reason = verify(configured_secret(), user, ts, raw, signature)
    if not ok:
        print(f"[api] 内部调用被拒绝：{reason}", flush=True)
        raise HTTPException(status_code=401, detail="内部调用身份校验失败")
    if not user:
        return None
    try:
        return int(user)
    except ValueError:
        raise HTTPException(status_code=400, detail="X-Rag-User 必须是数字") from None


def _guard(user_id: int | None) -> None:
    """按 userId 限流。放在检索之前：被限的请求不该消耗 embedding/rerank 额度。"""
    allowed, retry_ms = get_limiter().allow(user_id)
    if not allowed:
        wait = max(1, round(retry_ms / 1000))
        raise HTTPException(
            status_code=429,
            detail="提问太快了，请稍后再试",
            headers={"Retry-After": str(wait)},
        )


def _prefix(event_id: int | None) -> str | None:
    """老索引没有 event_id metadata 时的退路：按文件名前缀过滤。"""
    return f"E{event_id}-" if event_id else None


def _scope(req: AskRequest) -> dict | None:
    return {"event_id": req.eventId} if req.eventId is not None else None


def _body(answer_obj, cached: bool = False) -> dict:
    text = answer_obj.answer or ""
    debug = getattr(answer_obj, "debug", {}) or {}
    check = debug.get("citation_check") or {}
    out_of_range = check.get("out_of_range") or []
    llm_error = debug.get("llm_error")
    return {
        "answer": text,
        "citations": _citations(answer_obj.contexts or []),
        "refused": _looks_refused(text),
        # 引用越界说明模型编了不存在的来源编号，算降级信号
        "degraded": bool(out_of_range or llm_error),
        "reason": llm_error or (f"citation_out_of_range:{out_of_range}" if out_of_range else ""),
        "cached": cached,
        "contexts": len(answer_obj.contexts or []),
    }


def _citations(contexts) -> list[dict]:
    from rag.pipeline import citations_payload

    return citations_payload(contexts)


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz() -> dict:
    ready = _pipeline is not None
    body = {"status": "ok" if ready else "not_ready", "app": APP_NAME, "error": _pipeline_error}
    if ready:
        body.update(_pipeline.stats())
    body["cache"] = get_cache().stats()["backend"]
    body["rateLimit"] = get_limiter().stats()["backend"]
    return body


@app.get("/v1/stats")
def stats() -> dict:
    return {
        "pipeline": get_pipeline().stats(),
        "cache": get_cache().stats(),
        "rateLimit": get_limiter().stats(),
    }


@app.post("/v1/ask")
async def ask(
    request: Request,
    x_rag_user: str | None = Header(default=None),
    x_rag_ts: str | None = Header(default=None),
    x_rag_sign: str | None = Header(default=None),
):
    raw = await request.body()
    user_id = _authorize(raw, x_rag_user, x_rag_ts, x_rag_sign)
    _guard(user_id)
    try:
        req = _normalize(AskRequest.model_validate_json(raw))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"请求体格式错误：{exc}") from exc

    t0 = time.perf_counter()
    cache = get_cache()
    if req.useCache:
        hit = cache.get(req.eventId, user_id, req.question)
        if hit:
            return {**hit, "cached": True, "latencyMs": 0}

    try:
        answer = get_pipeline().ask(
            req.question,
            mode=req.mode,
            where=_scope(req),
            source_prefix=_prefix(req.eventId),
            user_facts=req.userFacts,
        )
    except Exception as exc:
        # 检索层挂了就降级，不返回 500
        return {
            "answer": "赛事助手暂时不可用，请稍后再试。报名与支付不受影响。",
            "citations": [],
            "degraded": True,
            "reason": f"retrieve_failed: {type(exc).__name__}",
            "refused": True,
            "cached": False,
            "contexts": 0,
            "latencyMs": int((time.perf_counter() - t0) * 1000),
        }

    body = _body(answer)
    body["latencyMs"] = int((time.perf_counter() - t0) * 1000)
    body["userScoped"] = bool(req.userFacts)
    # 降级结果不缓存：否则一次 LLM 抖动会被放大成 5 分钟的坏答案
    if req.useCache and not body["degraded"] and body["answer"]:
        cache.set(req.eventId, user_id, req.question, body)
    return body


@app.post("/v1/ask/stream")
async def ask_stream(
    request: Request,
    x_rag_user: str | None = Header(default=None),
    x_rag_ts: str | None = Header(default=None),
    x_rag_sign: str | None = Header(default=None),
):
    raw = await request.body()
    user_id = _authorize(raw, x_rag_user, x_rag_ts, x_rag_sign)
    _guard(user_id)
    try:
        req = _normalize(AskRequest.model_validate_json(raw))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"请求体格式错误：{exc}") from exc

    return StreamingResponse(
        _stream_events(req, user_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            # 关掉 nginx 的响应缓冲，否则 SSE 会被攒成一整块再发，等于没流式
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _stream_events(req: AskRequest, user_id: int | None):
    t0 = time.perf_counter()
    cache = get_cache()

    if req.useCache:
        hit = cache.get(req.eventId, user_id, req.question)
        if hit:
            yield _sse("meta", {"citations": hit.get("citations", []), "cached": True})
            yield _sse("delta", {"text": hit.get("answer", "")})
            yield _sse("done", {**hit, "cached": True, "latencyMs": 0})
            return

    try:
        for event, data in get_pipeline().stream(
            req.question,
            mode=req.mode,
            where=_scope(req),
            source_prefix=_prefix(req.eventId),
            user_facts=req.userFacts,
        ):
            if event == "done":
                citations = data.get("citations", [])
                body = {
                    "answer": data.get("answer", ""),
                    "citations": citations,
                    "contexts": len(citations),
                    "refused": data.get(
                        "refused", _looks_refused(data.get("answer", ""))
                    ),
                    "degraded": bool(data.get("llmError")),
                    "reason": data.get("llmError") or "",
                    "cached": False,
                    "userScoped": bool(req.userFacts),
                    "latencyMs": int((time.perf_counter() - t0) * 1000),
                }
                if req.useCache and not body["degraded"] and body["answer"]:
                    cache.set(req.eventId, user_id, req.question, body)
                yield _sse("done", body)
            elif event == "delta":
                yield _sse("delta", data)
            elif event == "meta":
                yield _sse("meta", data)
            else:
                yield _sse("error", data)
    except Exception as exc:
        print(f"[api] stream 失败：{exc}", flush=True)
        yield _sse("error", {"message": "赛事助手暂时不可用，请稍后再试。"})
        yield _sse(
            "done",
            {
                "answer": "",
                "citations": [],
                "refused": True,
                "degraded": True,
                "reason": f"retrieve_failed: {type(exc).__name__}",
                "cached": False,
                "latencyMs": int((time.perf_counter() - t0) * 1000),
            },
        )
