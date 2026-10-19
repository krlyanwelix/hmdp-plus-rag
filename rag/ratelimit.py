"""应用层限流：按 userId 的令牌桶，放在检索/生成之前。

为什么在 Python 侧还要做一层：Java 侧那层是**滑动窗口**，它的职责是「单用户
请求频次」，窗口内计数、边界处可以瞬间翻倍；这里补的是**突发配额**：允许
攒 5 个令牌一次用掉（用户连点三下不该被拦），但不允许每分钟超过 20 次。
两层目标不同，不是重复建设。

为什么用令牌桶而不是固定窗口：固定窗口在窗口切换的那一刻会放行 2 倍配额
（59 秒打满、61 秒再打满）。令牌桶按匀速补充，没有这个问题。

为什么 fail-open：限流是防御性的，Redis 挂了就让请求过去。反过来（fail-close）
会让一次缓存故障升级成整个助手不可用，代价远大于被刷几下。
"""

from __future__ import annotations

import os
import threading
import time

# KEYS[1] 桶 key
# ARGV[1] 容量  ARGV[2] 每秒补充速率
# 返回 {是否放行, 剩余令牌(千分之一), 建议等待毫秒}
_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])

-- 用到 TIME 这类非确定性命令后，脚本默认被判为"不可复制"，再执行写命令会报
-- "Write commands not allowed after non deterministic commands"（Redis 3.2 上必现）。
-- 声明按效果复制即可（Redis 7 里是默认行为，这行是空操作）。
redis.replicate_commands()

local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])

if tokens == nil or ts == nil then
  tokens = capacity
  ts = now
end

local delta = math.max(0, now - ts)
tokens = math.min(capacity, tokens + delta * rate)

local allowed = 0
local retry = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry = math.ceil((1 - tokens) / rate * 1000)
end

-- 本机 Redis 是 3.2.100（Windows 老版本），HSET 的 arity 是 4，不支持一次写多个
-- field-value（多对写入是 Redis 4.0 才加的），否则报
-- "Wrong number of args calling Redis command From Lua script"。
-- 同理浮点数要自己格式化，Lua 的 number 直接当参数会报 "must be strings or integers"。
redis.call('HSET', key, 'tokens', string.format('%.6f', tokens))
redis.call('HSET', key, 'ts', string.format('%.6f', now))
redis.call('PEXPIRE', key, string.format('%d', math.floor(capacity / rate * 1000) + 2000))
return {allowed, math.floor(tokens * 1000), retry}
"""


class RateLimiter:
    def __init__(
        self,
        redis_url: str | None = None,
        per_min: int = 20,
        burst: int = 5,
        enabled: bool = True,
        namespace: str = "rag:rl",
    ):
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        self.enabled = bool(enabled)
        self.namespace = namespace
        self.per_min = max(1, int(per_min))
        self.capacity = max(1, int(burst))
        self.rate = self.per_min / 60.0
        self._client = None
        self._script = None
        self._dead_until = 0.0
        self._lock = threading.Lock()
        self.allowed = 0
        self.blocked = 0
        self.errors = 0
        # 预热：启动时就把连接建好，免得第一个请求既付连接延迟、healthz 又报 unavailable
        if self.enabled:
            self._redis()

    # ------------------------------------------------------------------ #
    def _redis(self):
        if not self.enabled:
            return None
        if self._client is not None:
            return self._client
        if time.time() < self._dead_until:
            return None
        with self._lock:
            if self._client is not None:
                return self._client
            if time.time() < self._dead_until:
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
                self._script = client.register_script(_LUA)
            except Exception:
                self._dead_until = time.time() + 30
                self._client = None
                self._script = None
        return self._client

    def allow(self, identity: str | int | None) -> tuple[bool, int]:
        """返回 (是否放行, 建议等待毫秒)。Redis 不可用时永远放行。"""
        if not self.enabled:
            return True, 0
        client = self._redis()
        if client is None or self._script is None:
            return True, 0
        key = f"{self.namespace}:{identity if identity is not None else 'anon'}"
        try:
            allowed, _, retry = self._script(
                keys=[key], args=[self.capacity, self.rate], client=client
            )
        except Exception as exc:
            # 限流脚本失败不能挡住正常请求
            print(f"[ratelimit] 脚本执行失败，本次放行：{type(exc).__name__}: {exc}", flush=True)
            self.errors += 1
            self._dead_until = time.time() + 30
            self._client = None
            self._script = None
            return True, 0
        if int(allowed) == 1:
            self.allowed += 1
            return True, 0
        self.blocked += 1
        return False, max(200, int(retry))

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "backend": "redis" if self._client is not None else "unavailable",
            "per_min": self.per_min,
            "burst": self.capacity,
            "allowed": self.allowed,
            "blocked": self.blocked,
            "errors": self.errors,
        }
