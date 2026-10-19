"""LLM 客户端。

用标准库 urllib 直连 OpenAI 兼容接口，不引入 requests/openai 依赖。
任何兼容 /v1/chat/completions 的服务都能用：DeepSeek、通义、Moonshot、vLLM、Ollama。

没配 API key 时自动降级为 EchoLLM —— 它不生成答案，只把检索到的上下文原样展示，
这样在没有 LLM 的情况下也能验证「检索这一步做对了没有」。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

from rag.types import Context


class LLM(ABC):
    name: str = "base"

    @abstractmethod
    def chat(self, messages: list[dict], **kwargs) -> str:
        ...

    def chat_stream(self, messages: list[dict], **kwargs):
        """流式输出。默认实现退化为一次性吐出全文。

        上层（SSE 接口）只依赖这个迭代器，所以不支持流式的模型也能接，
        代价是首字延迟等于整段生成时间。
        """
        yield self.chat(messages, **kwargs)


class OpenAICompatLLM(LLM):
    name = "openai-compatible"

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        temperature: float = 0.1,
        timeout: int = 60,
        stream_timeout: int | None = None,
    ):
        if not api_key:
            raise ValueError("缺少 API key")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        # 流式要单独设：非流式的 45s 是「整段生成」的上限，流式如果也取 45s，
        # 上游卡住时用户要盯着转圈 45 秒才看到错误。20s 无字节到达就该断了。
        self.stream_timeout = int(stream_timeout or timeout)
        # 最近一次调用的 token 用量（由 chat 填充），用于成本核算
        self.last_usage: dict = {}

    def chat(self, messages: list[dict], **kwargs) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.pop("temperature", self.temperature),
            **kwargs,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"LLM 调用失败 HTTP {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接 LLM 服务 {self.base_url}：{exc.reason}") from exc

        self.last_usage = body.get("usage") or {}
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError(f"LLM 返回格式异常：{str(body)[:300]}")
        return choices[0].get("message", {}).get("content", "")

    def chat_stream(self, messages: list[dict], **kwargs):
        """流式版 chat，逐段 yield 增量文本。

        用 urllib 直接读 SSE：`for line in resp` 是行缓冲的，服务端每推一行就能拿到，
        不需要 websocket，也不需要额外依赖。
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.pop("temperature", self.temperature),
            "stream": True,
            **kwargs,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=self.stream_timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"LLM 流式调用失败 HTTP {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接 LLM 服务 {self.base_url}：{exc.reason}") from exc

        try:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                piece = (choices[0].get("delta") or {}).get("content")
                if piece:
                    yield piece
        finally:
            resp.close()


class EchoLLM(LLM):
    """无 API key 时的降级：不做生成，只回显检索结果。"""

    name = "echo"

    def __init__(self, contexts_provider=None):
        self.contexts_provider = contexts_provider
        self.last_usage: dict = {}  # 回显模式不产生 token 消耗

    def chat(self, messages: list[dict], **kwargs) -> str:
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        contexts: list[Context] = self.contexts_provider() if self.contexts_provider else []
        lines = [
            "（未配置 LLM API key，以下是检索到的原文，用于验证检索质量）",
            "",
        ]
        for i, ctx in enumerate(contexts, start=1):
            head = f"[{i}] {ctx.source}"
            if ctx.metadata.get("heading"):
                head += f" ｜ {ctx.metadata['heading']}"
            lines.append(head)
            lines.append(ctx.text.strip()[:600])
            lines.append("")
        lines.append(f"—— 上述内容用于回答：{user.splitlines()[-1] if user else ''}")
        return "\n".join(lines)

    def chat_stream(self, messages: list[dict], **kwargs):
        # 回显模式没有真正的 token 流，按行切开模拟，让 SSE 链路仍可被测试
        for line in self.chat(messages, **kwargs).splitlines(keepends=True):
            yield line


def get_llm(cfg, contexts_provider=None, verbose: bool = True) -> LLM:
    if not cfg.llm_api_key:
        if verbose:
            print("[llm] 未配置 API key，使用回显模式（只展示检索结果，不生成答案）")
        return EchoLLM(contexts_provider=contexts_provider)

    try:
        llm = OpenAICompatLLM(
            model=cfg.llm_model,
            base_url=cfg.llm_base_url,
            api_key=cfg.llm_api_key,
            temperature=cfg.llm_temperature,
            timeout=cfg.llm_timeout,
            stream_timeout=getattr(cfg, "llm_stream_timeout", None),
        )
        if verbose:
            print(f"[llm] {cfg.llm_model} @ {cfg.llm_base_url}")
        return llm
    except Exception as exc:
        if verbose:
            print(f"[llm] 初始化失败（{exc}），使用回显模式")
        return EchoLLM(contexts_provider=contexts_provider)
