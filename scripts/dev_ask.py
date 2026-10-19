"""带签名的调试客户端。

用途：在不开 Java 的情况下验证 RAG 服务（含流式、缓存、隔离）。

用法：
    E:\\Python312\\python.exe scripts\\dev_ask.py "E2 的 50 公里组要带头灯吗" --event 2
    E:\\Python312\\python.exe scripts\\dev_ask.py "我报名成功了吗" --event 1 --user 1 --fact "报名单 DJ2026...：50 公里组，状态=待支付"
    E:\\Python312\\python.exe scripts\\dev_ask.py "..." --event 1 --no-stream

签名口径必须和 service/security.py 一致：
    HMAC-SHA256(secret, "{user}\\n{ts}\\n" || sha256(body))
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".pylibs"))
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，答案里有 ⚠ 之类的符号会直接 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from config import Config  # noqa: F401,E402  作用：加载 .env 到 os.environ

SECRET = os.getenv("RAG_SHARED_SECRET", "")


def sign(user: str, ts: str, body: bytes) -> str:
    message = f"{user}\n{ts}\n".encode("utf-8") + hashlib.sha256(body).digest()
    return hmac.new(SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()


def build_request(base: str, path: str, payload: dict, user: str | None, bad_sign: bool = False):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if user:
        ts = str(int(time.time()))
        sig = sign(user, ts, body)
        if bad_sign:
            sig = "0" * len(sig)
        headers.update({"X-Rag-User": user, "X-Rag-Ts": ts, "X-Rag-Sign": sig})
    return urllib.request.Request(base + path, data=body, headers=headers, method="POST")


def run_stream(base: str, req) -> int:
    started = time.perf_counter()
    first_delta = None
    with urllib.request.urlopen(req, timeout=60) as resp:
        for raw in resp:
            line = raw.decode("utf-8").rstrip()
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            data = json.loads(line[5:].strip())
            elapsed = int((time.perf_counter() - started) * 1000)
            if event == "meta":
                srcs = [c["source"] for c in data.get("citations", [])]
                print(f"[meta {elapsed:>4}ms] 命中 {data.get('contexts')} 段：{srcs}")
            elif event == "delta":
                if first_delta is None:
                    first_delta = elapsed
                    print(f"[首字 {elapsed}ms] ", end="")
                print(data.get("text", ""), end="", flush=True)
            elif event == "error":
                print(f"\n[error] {data.get('message')}")
            elif event == "done":
                print()
                print(
                    f"[done] {elapsed}ms refused={data.get('refused')} "
                    f"degraded={data.get('degraded')} cached={data.get('cached')} "
                    f"citations={len(data.get('citations', []))}"
                )
    if first_delta is not None:
        print(f"[首字延迟 {first_delta}ms / 总 {int((time.perf_counter() - started) * 1000)}ms]")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--base", default="http://127.0.0.1:8090")
    ap.add_argument("--event", type=int, default=None)
    ap.add_argument("--user", default=None, help="要模拟的业务用户 id")
    ap.add_argument("--fact", action="append", default=[], help="用户档案，可多次给")
    ap.add_argument("--no-stream", action="store_true")
    ap.add_argument("--bad-sign", action="store_true", help="故意用错签名，验证 401")
    args = ap.parse_args()

    payload = {
        "question": args.question,
        "eventId": args.event,
        "userFacts": args.fact or None,
        "useCache": True,
    }
    path = "/v1/ask" if args.no_stream else "/v1/ask/stream"
    req = build_request(args.base, path, payload, args.user, args.bad_sign)

    try:
        if args.no_stream:
            started = time.perf_counter()
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            ms = int((time.perf_counter() - started) * 1000)
            print(json.dumps(body, ensure_ascii=False, indent=2))
            print(f"[{ms}ms]")
            return 0
        return run_stream(args.base, req)
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
