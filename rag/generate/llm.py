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


class OpenAICompatLLM(LLM):
    name = "openai-compatible"

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        temperature: float = 0.1,
        timeout: int = 60,
    ):
        if not api_key:
            raise ValueError("缺少 API key")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout

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

        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError(f"LLM 返回格式异常：{str(body)[:300]}")
        return choices[0].get("message", {}).get("content", "")


class EchoLLM(LLM):
    """无 API key 时的降级：不做生成，只回显检索结果。"""

    name = "echo"

    def __init__(self, contexts_provider=None):
        self.contexts_provider = contexts_provider

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
        )
        if verbose:
            print(f"[llm] {cfg.llm_model} @ {cfg.llm_base_url}")
        return llm
    except Exception as exc:
        if verbose:
            print(f"[llm] 初始化失败（{exc}），使用回显模式")
        return EchoLLM(contexts_provider=contexts_provider)
