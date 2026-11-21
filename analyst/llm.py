"""LLM backends behind one small interface.

Every LLM call in the pipeline asks for JSON that matches a schema:

    reply = llm.json(task="parse", system=..., user=..., schema=..., tier="fast", context={...})

* `AnthropicLLM` calls Claude through the official SDK with structured outputs
  (`output_config.format`), so the reply is always schema-valid JSON. The stable
  system prompt (rules + role-filtered catalog) is marked for prompt caching.
* `OfflineLLM` is a deterministic, rule-based stand-in. It reads the structured
  `context` instead of the prompt text. It lets the whole pipeline run (and be
  tested) without network access or an API key, and gives a reproducible baseline.
* `ScriptedLLM` replays canned replies; tests use it to drive the repair loop,
  malformed output and prompt-injection paths.

The pipeline never trusts the reply: it re-validates every name against the
semantic layer and every SQL statement through the safety gate.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class LLMError(RuntimeError):
    """The model could not produce a usable reply (refusal, truncation, provider down)."""


@dataclass
class LLMReply:
    data: dict
    model: str
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    raw: str = ""

    def usage(self) -> dict:
        return {"model": self.model, "latency_ms": self.latency_ms, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "cache_read_tokens": self.cache_read_tokens}


class LLM(Protocol):
    name: str

    def json(self, task: str, system: str, user: str, schema: dict, tier: str = "fast",
             context: dict | None = None) -> LLMReply: ...


# --------------------------------------------------------------------------- Anthropic
FALLBACK_CAPABLE = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1", "claude-sonnet-5-5")


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, settings: dict, client=None):
        import anthropic
        self._anthropic = anthropic
        self.cfg = settings["llm"]
        self.client = client or anthropic.Anthropic()

    def _model(self, tier: str) -> tuple[str, str]:
        if tier == "fast":
            return self.cfg["model_fast"], self.cfg.get("effort_fast", "low")
        return self.cfg["model_reasoning"], self.cfg.get("effort_reasoning", "medium")

    def json(self, task, system, user, schema, tier="fast", context=None) -> LLMReply:
        a = self._anthropic
        model, effort = self._model(tier)
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=self.cfg.get("max_tokens", 16000),
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        started = time.perf_counter()
        try:
            fb = self.cfg.get("refusal_fallback")
            if fb and model in FALLBACK_CAPABLE:
                resp = self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                        fallbacks=fb, **kwargs)
            else:
                resp = self.client.messages.create(**kwargs)
        except a.RateLimitError as e:
            raise LLMError(f"rate limited by the LLM provider: {e.message}") from e
        except a.APIStatusError as e:
            raise LLMError(f"LLM provider error {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise LLMError("cannot reach the LLM provider") from e
        ms = int((time.perf_counter() - started) * 1000)
        if resp.stop_reason == "refusal":
            raise LLMError("the model declined this request")
        if resp.stop_reason == "max_tokens":
            raise LLMError("the model's reply was cut off (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"model returned invalid JSON: {e}") from e
        u = resp.usage
        return LLMReply(data, resp.model, ms, u.input_tokens, u.output_tokens,
                        getattr(u, "cache_read_input_tokens", 0) or 0, text)


# --------------------------------------------------------------------------- offline / scripted
class OfflineLLM:
    """Deterministic backend. Each task is a plain function of the structured context."""
    name = "offline"

    def __init__(self, handlers: dict[str, Callable[[dict], dict]] | None = None):
        self.handlers = handlers or {}

    def json(self, task, system, user, schema, tier="fast", context=None) -> LLMReply:
        if task not in self.handlers:
            raise LLMError(f"the offline backend cannot do {task!r}; configure an LLM provider")
        started = time.perf_counter()
        data = self.handlers[task](context or {})
        return LLMReply(data, "offline-rules", int((time.perf_counter() - started) * 1000), raw=json.dumps(data))


@dataclass
class ScriptedLLM:
    """Replays replies per task, in order. A reply may be a dict, a JSON string, or an Exception.
    Falls back to `fallback` (e.g. an OfflineLLM) when a task has no scripted replies left."""
    replies: dict[str, list] = field(default_factory=dict)
    fallback: Any = None
    calls: list[dict] = field(default_factory=list)
    name: str = "scripted"

    def json(self, task, system, user, schema, tier="fast", context=None) -> LLMReply:
        self.calls.append({"task": task, "user": user, "tier": tier})
        queue = self.replies.get(task)
        if not queue:
            if self.fallback is None:
                raise LLMError(f"no scripted reply for {task!r}")
            return self.fallback.json(task, system, user, schema, tier, context)
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, str):
            try:
                r = json.loads(r)
            except json.JSONDecodeError as e:
                raise LLMError(f"model returned invalid JSON: {e}") from e
        return LLMReply(r, "scripted", raw=json.dumps(r))


def has_anthropic_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    home = os.path.expanduser("~/.config/anthropic")
    return os.path.isdir(home) and any(os.scandir(home))


def make_llm(settings: dict, offline_handlers: dict | None = None, provider: str | None = None) -> LLM:
    provider = provider or settings["llm"].get("provider", "auto")
    if provider == "auto":
        provider = "anthropic" if has_anthropic_credentials() else "offline"
    if provider == "anthropic":
        return AnthropicLLM(settings)
    if provider == "offline":
        return OfflineLLM(offline_handlers)
    raise ValueError(f"unknown llm provider {provider!r}")
