"""内部调用签名。

RAG 服务只被 Java 后端调用，**用户身份必须来自 Java 侧的会话**（UserHolder），
绝不能由浏览器直接传 userId —— 否则任何人改个数字就能查别人的报名状态。

为了让「Java 说是谁就是谁」这件事可验证，两边共享一个密钥：
    X-Rag-User  业务用户 id
    X-Rag-Ts    unix 秒
    X-Rag-Sign  HMAC-SHA256(secret, "{user}\\n{ts}\\n" || sha256(body))

Python 侧验签 + 校验时间窗（默认 60s），既防伪造也防重放。

没配密钥时进入 dev 模式：放行并打警告，方便本地 curl 调试；
生产环境必须配 `RAG_SHARED_SECRET`，否则任何人都能伪造身份。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time


def sign(secret: str, user: str, ts: str, body: bytes) -> str:
    message = f"{user}\n{ts}\n".encode("utf-8") + hashlib.sha256(body).digest()
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify(
    secret: str,
    user: str | None,
    ts: str | None,
    body: bytes,
    signature: str | None,
    ttl: int = 60,
    now: int | None = None,
) -> tuple[bool, str]:
    """返回 (是否通过, 原因)。失败原因只用于日志，不回给调用方。"""
    if not secret:
        return True, "dev-mode: 未配置 RAG_SHARED_SECRET，跳过验签"
    if not user or not ts or not signature:
        return False, "缺少 X-Rag-User / X-Rag-Ts / X-Rag-Sign"
    try:
        ts_value = int(ts)
    except (TypeError, ValueError):
        return False, "时间戳格式非法"

    current = int(now if now is not None else time.time())
    if abs(current - ts_value) > ttl:
        return False, f"时间戳超出 ±{ttl}s 窗口（疑似重放）"

    expected = sign(secret, user, ts, body)
    if not hmac.compare_digest(expected, signature.lower()):
        return False, "签名不匹配"
    return True, "ok"


def configured_secret() -> str:
    return os.getenv("RAG_SHARED_SECRET", "").strip()
