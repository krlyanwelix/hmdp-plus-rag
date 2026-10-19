"""答案缓存：Redis 为主，进程内 LRU 兜底。

为什么要跨实例：RAG 服务是多进程/多机部署的，进程内字典只能让「同一个实例」
的重复提问省下调用，负载均衡一转发就打不中了。放 Redis 才能让 8090 上算出的
答案在 8091 上直接命中。

为什么要版本号：重建索引后，旧答案里引用的分块已经不存在了，只靠 TTL 淘汰会让
过期答案再活 5 分钟。用一个全局版本号做 key 前缀，重建时 INCR 一次，旧 key 立刻
不可达（剩下的由 TTL 自然回收）—— 这也避免了 SCAN + DEL 的昂贵操作。

设计约束：缓存**不是正确性依赖**。Redis 挂掉、超时、返回脏数据，都必须退化为
「没命中」继续走完整链路，绝不能让接口失败。

为什么要加密：带 userFacts 的答案里可能有「报名单 6503… 已支付」这类个人信息，
Redis 默认没有鉴权、运维能直接 GET、备份文件会流到别处。key 里带 userId 只能
防串号，防不了「拿到 dump 的人」。所以落盘前 AES-GCM 加密，key 由
RAG_CACHE_KEY 或共享密钥派生；含个人数据的答案 TTL 也单独压到 60 秒。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from typing import Any

PREFIX = "rag:ans:"
VERSION_KEY = "rag:corpus:version"
DEFAULT_TTL = 300
DEFAULT_USER_TTL = 60
DEFAULT_LOCAL_SIZE = 512
# 密文前缀：既用来判断是否加密，也留了升级到 v2 的余地
ENC_PREFIX = "e1:"


def _digest(question: str) -> str:
    return hashlib.sha1(question.strip().lower().encode("utf-8")).hexdigest()[:20]


def derive_key(material: str | None) -> bytes:
    """把任意字符串派生成 32 字节 AES 密钥（HKDF-SHA256）。

    直接从口令当密钥是不行的：口令熵低、且同一口令在别的系统里可能也用过。
    HKDF 加了 salt 和 info，同样的输入在不同用途下派生出不同的子密钥。
    """
    raw = (material or "yejing-rag-dev-key").encode("utf-8")
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"rag-cache-v1",
            info=b"answer-cache",
        ).derive(raw)
    except Exception:
        # 没装 cryptography 时退化为加盐哈希：仍比明文强，但不可与 HKDF 互操作
        return hashlib.sha256(b"rag-cache-v1" + raw).digest()


class AnswerCache:
    def __init__(
        self,
        redis_url: str | None = None,
        ttl: int = DEFAULT_TTL,
        enabled: bool = True,
        local_size: int = DEFAULT_LOCAL_SIZE,
        namespace: str = "rag",
        encrypt: bool = True,
        key: str | None = None,
        user_ttl: int = DEFAULT_USER_TTL,
    ):
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        self.ttl = max(1, int(ttl))
        self.user_ttl = max(1, int(user_ttl))
        self.enabled = enabled
        self.namespace = namespace
        self._local: OrderedDict[str, dict] = OrderedDict()
        self._local_size = max(16, int(local_size))
        self._client: Any = None
        self._redis_dead_until = 0.0
        self._version = 0
        self._version_at = 0.0
        self._lock = threading.Lock()
        self._aes: Any = None
        self.encrypt = bool(encrypt)
        self._key_material = key or os.getenv("RAG_CACHE_KEY") or os.getenv("RAG_SHARED_SECRET")
        if self.encrypt and self._aes is None:
            self._init_cipher()
        self.hits = 0
        self.misses = 0
        self.errors = 0

    def _init_cipher(self) -> None:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            self._aes = AESGCM(derive_key(self._key_material))
        except Exception as exc:
            # 宁可明文也不让服务起不来：加密是加固项，不是启动依赖
            print(f"[cache] 加密不可用（{type(exc).__name__}: {exc}），回退明文存储", flush=True)
            self.encrypt = False
            self._aes = None

    def _encode(self, payload: dict) -> str:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if not self.encrypt or self._aes is None:
            return body.decode("utf-8")
        nonce = os.urandom(12)
        # AAD 绑定命名空间：同一份密文搬到别的用途上解不开
        ct = self._aes.encrypt(nonce, body, self.namespace.encode("utf-8"))
        return ENC_PREFIX + base64.b64encode(nonce + ct).decode("ascii")

    def _decode(self, raw: str) -> dict | None:
        raw = (raw or "").strip()
        if not raw:
            return None
        if not raw.startswith(ENC_PREFIX):
            return json.loads(raw)
        if self._aes is None:
            return None
        blob = base64.b64decode(raw[len(ENC_PREFIX) :])
        plain = self._aes.decrypt(blob[:12], blob[12:], self.namespace.encode("utf-8"))
        return json.loads(plain.decode("utf-8"))

    @classmethod
    def from_env(cls, namespace: str = "rag") -> "AnswerCache":
        enabled = os.getenv("RAG_CACHE_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
        ttl = int(os.getenv("RAG_CACHE_TTL", str(DEFAULT_TTL)))
        user_ttl = int(os.getenv("RAG_CACHE_USER_TTL", str(DEFAULT_USER_TTL)))
        encrypt = os.getenv("RAG_CACHE_ENCRYPT", "true").strip().lower() not in {
            "0",
            "false",
            "no",
        }
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            ttl=ttl,
            user_ttl=user_ttl,
            enabled=enabled,
            encrypt=encrypt,
            namespace=namespace,
        )

    # ------------------------------------------------------------------ #
    # Redis 连接（惰性 + 熔断）
    # ------------------------------------------------------------------ #
    def _redis(self):
        """拿连接。连续失败后退避 30s，避免每次请求都白等一次超时。

        socket_timeout 必须给得很短：缓存是可选路径，不能让它拖慢主链路。
        """
        if not self.enabled:
            return None
        if self._client is not None:
            return self._client
        if time.time() < self._redis_dead_until:
            return None
        with self._lock:
            if self._client is not None:
                return self._client
            if time.time() < self._redis_dead_until:
                return None
            try:
                import redis  # type: ignore

                client = redis.Redis.from_url(
                    self.redis_url,
                    socket_timeout=0.3,
                    socket_connect_timeout=0.3,
                    decode_responses=True,
                )
                client.ping()
                self._client = client
            except Exception:
                self._redis_dead_until = time.time() + 30
                self._client = None
        return self._client

    def _namespace_version(self) -> int:
        """读全局索引版本；本地缓存 5 秒，省掉每请求一次 RTT。"""
        now = time.time()
        if now - self._version_at < 5:
            return self._version
        client = self._redis()
        if client is not None:
            try:
                raw = client.get(VERSION_KEY)
                self._version = int(raw) if raw else 0
                self._version_at = now
            except Exception:
                self.errors += 1
                self._redis_dead_until = time.time() + 30
                self._client = None
        return self._version

    def bump_version(self) -> int:
        """索引重建后调用，整体失效。Redis 不可用时只清本地缓存。"""
        self._version_at = 0.0
        with self._lock:
            self._local.clear()
        client = self._redis()
        if client is None:
            self._version += 1
            return self._version
        try:
            self._version = int(client.incr(VERSION_KEY))
            self._version_at = time.time()
            return self._version
        except Exception:
            self.errors += 1
            self._redis_dead_until = time.time() + 30
            self._client = None
            self._version += 1
            return self._version

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #
    def key(self, event_id: int | None, user_id: int | None, question: str) -> str:
        return (
            f"{self.namespace}:ans:{self._namespace_version()}:"
            f"{event_id or 0}:{user_id or 0}:{_digest(question)}"
        )

    def get(self, event_id: int | None, user_id: int | None, question: str) -> dict | None:
        if not self.enabled:
            return None
        key = self.key(event_id, user_id, question)
        client = self._redis()
        if client is not None:
            try:
                raw = client.get(key)
                if raw:
                    payload = self._decode(raw)
                    if payload is not None:
                        self.hits += 1
                        return payload
                    # 解不开多半是换了密钥或存了旧格式：删掉当未命中，别反复踩
                    self.errors += 1
                    client.delete(key)
            except Exception:
                self.errors += 1
                self._redis_dead_until = time.time() + 30
                self._client = None
        with self._lock:
            payload = self._local.get(key)
            if payload is not None:
                self._local.move_to_end(key)
                self.hits += 1
                return payload
        self.misses += 1
        return None

    def set(
        self,
        event_id: int | None,
        user_id: int | None,
        question: str,
        payload: dict,
    ) -> None:
        if not self.enabled:
            return
        key = self.key(event_id, user_id, question)
        # 带个人数据的答案不按普通 TTL 留：这类内容一旦泄漏代价高，且时效性强
        ttl = self.user_ttl if payload.get("userScoped") else self.ttl
        client = self._redis()
        if client is not None:
            try:
                client.setex(key, ttl, self._encode(payload))
            except Exception:
                self.errors += 1
                self._redis_dead_until = time.time() + 30
                self._client = None
        with self._lock:
            self._local[key] = payload
            self._local.move_to_end(key)
            while len(self._local) > self._local_size:
                self._local.popitem(last=False)

    def stats(self) -> dict:
        client = self._redis()
        return {
            "enabled": self.enabled,
            "backend": "redis" if client is not None else "local",
            "redis_url": self.redis_url if client is not None else None,
            "version": self._version,
            "ttl": self.ttl,
            "user_ttl": self.user_ttl,
            "encrypted": bool(self.encrypt and self._aes is not None),
            "hits": self.hits,
            "misses": self.misses,
            "errors": self.errors,
            "local_size": len(self._local),
        }
