"""LLM backends behind one small interface.

Every LLM call in the pipeline asks for JSON that matches a schema:

    reply = llm.json(task="parse", system=..., user=..., schema=..., tier="fast", context={...})

* `AnthropicLLM` calls Claude through the official SDK with structured outputs
  (`output_config.format`), so the reply is always schema-valid JSON. The stable
  system prompt (rules + role-filtered catalog) is marked for prompt caching.
* `GeminiLLM` calls Google's Gemini REST API (AI Studio key in GEMINI_API_KEY) with
  JSON-schema structured output, retrying on rate limits and temporary overload.
* `OpenRouterLLM` calls any OpenRouter model (OPENROUTER_API_KEY) through its OpenAI-compatible API,
  with a hard per-process call cap for free quotas.
* `FallbackLLM` wraps a remote backend and answers from the offline rules when it fails (demos).
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
import re
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


# --------------------------------------------------------------------------- Google Gemini (REST)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_DECLINED = ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY")


def gemini_api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


class GeminiLLM:
    """Gemini through its REST API with `responseJsonSchema`, so replies are schema-shaped JSON.

    Uses httpx (no extra SDK). Rate limits (429) and overload (500/503) are retried with the
    server's suggested delay or exponential backoff; a daily quota that is used up fails at once.
    Thinking tokens are billed as output, so they are counted as output tokens."""
    name = "gemini"

    def __init__(self, settings: dict, client=None, api_key: str | None = None, sleep=time.sleep):
        import httpx
        self.cfg = settings["llm"].get("gemini", {})
        self.key = api_key or gemini_api_key()
        if not self.key:
            raise LLMError("set GEMINI_API_KEY to use the Gemini backend")
        self.client = client or httpx.Client(timeout=float(self.cfg.get("timeout_s", 120)))
        self.max_retries = int(self.cfg.get("max_retries", 5))
        self.sleep = sleep

    def _model(self, tier: str) -> str:
        return self.cfg.get("model_fast" if tier == "fast" else "model_reasoning", "gemini-3-flash-preview")

    def json(self, task, system, user, schema, tier="fast", context=None) -> LLMReply:
        model = self._model(tier)
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema,
                                 "maxOutputTokens": int(self.cfg.get("max_output_tokens", 16000))},
        }
        started = time.perf_counter()
        resp = self._post(model, body)
        ms = int((time.perf_counter() - started) * 1000)
        j = resp.json()
        cands = j.get("candidates") or []
        if not cands:
            reason = (j.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LLMError(f"the model declined this request ({reason})")
        c = cands[0]
        finish = c.get("finishReason", "")
        if finish in GEMINI_DECLINED:
            raise LLMError(f"the model declined this request ({finish})")
        if finish == "MAX_TOKENS":
            raise LLMError("the model's reply was cut off (max tokens)")
        text = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts", []) if not p.get("thought"))
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"model returned invalid JSON: {e}") from e
        if not isinstance(data, dict):
            raise LLMError("model returned JSON that is not an object")
        u = j.get("usageMetadata") or {}
        cached = int(u.get("cachedContentTokenCount") or 0)
        return LLMReply(data, model, ms, int(u.get("promptTokenCount") or 0) - cached,
                        int(u.get("candidatesTokenCount") or 0) + int(u.get("thoughtsTokenCount") or 0), cached, text)

    def _post(self, model: str, body: dict):
        import httpx
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            try:
                r = self.client.post(GEMINI_URL.format(model=model), headers={"x-goog-api-key": self.key}, json=body)
            except httpx.HTTPError as e:
                if attempt == self.max_retries:
                    raise LLMError("cannot reach the LLM provider") from e
                self.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if r.status_code == 200:
                return r
            try:
                err = r.json().get("error", {})
            except ValueError:
                err = {"message": r.text[:200]}
            msg = err.get("message", "")
            retryable = r.status_code in (429, 500, 502, 503, 504)
            daily = r.status_code == 429 and ("PerDay" in json.dumps(err) or "per day" in msg.lower())
            if not retryable or daily or attempt == self.max_retries:
                kind = "quota used up" if daily else f"error {r.status_code}"
                raise LLMError(f"LLM provider {kind}: {msg[:200]}")
            wait = delay
            for d in err.get("details", []):
                m = re.fullmatch(r"([0-9.]+)s", str(d.get("retryDelay", "")))
                if m:
                    wait = float(m.group(1)) + 1
            self.sleep(min(wait, 90))
            delay = min(delay * 2, 60)
        raise LLMError("LLM provider unavailable")


# --------------------------------------------------------------------------- OpenRouter (OpenAI-compatible REST)
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class CallBudget:
    """A hard cap on real provider calls per process (OPENROUTER_MAX_CALLS / llm.<provider>.max_calls).
    Every HTTP request counts, retries included, so a free quota can't be spent by accident."""

    def __init__(self, limit: int | None):
        self.limit, self.used = limit, 0

    def take(self) -> None:
        if self.limit is not None and self.used >= self.limit:
            raise LLMError(f"call budget used up ({self.limit} calls)")
        self.used += 1


class OpenRouterLLM:
    """Any OpenRouter model through its OpenAI-compatible API, with JSON-schema structured output.

    `models` lets OpenRouter fall through to the next model when one is rate-limited or down; routing is
    restricted to providers that honour the response schema (`require_parameters`)."""
    name = "openrouter"

    def __init__(self, settings: dict, client=None, api_key: str | None = None, sleep=time.sleep):
        import httpx
        self.cfg = settings["llm"].get("openrouter", {})
        self.key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.key:
            raise LLMError("set OPENROUTER_API_KEY to use the OpenRouter backend")
        self.client = client or httpx.Client(timeout=float(self.cfg.get("timeout_s", 120)))
        self.max_retries = int(self.cfg.get("max_retries", 1))
        env_cap = os.environ.get("OPENROUTER_MAX_CALLS")
        cap = int(env_cap) if env_cap else self.cfg.get("max_calls")
        self.budget = CallBudget(int(cap) if cap is not None else None)
        self.sleep = sleep

    def _models(self, tier: str) -> list[str]:
        key = "models_fast" if tier == "fast" else "models_reasoning"
        models = self.cfg.get(key) or self.cfg.get("models") or ["openrouter/free"]
        return list(models)

    def json(self, task, system, user, schema, tier="fast", context=None) -> LLMReply:
        models = self._models(tier)
        body = {
            "model": models[0],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": re.sub(r"[^a-zA-Z0-9_-]", "_", task), "strict": True,
                                                "schema": schema}},
            "max_tokens": int(self.cfg.get("max_output_tokens", 8000)),
            "provider": {"require_parameters": True},
        }
        if len(models) > 1:
            body["models"] = models
        started = time.perf_counter()
        j = self._post(body)
        ms = int((time.perf_counter() - started) * 1000)
        choices = j.get("choices") or []
        if not choices:
            raise LLMError(f"LLM provider returned no answer: {str(j.get('error') or j)[:200]}")
        ch = choices[0]
        if ch.get("error"):
            raise LLMError(f"LLM provider error: {str(ch['error'])[:200]}")
        finish = ch.get("finish_reason") or ""
        if finish == "content_filter":
            raise LLMError("the model declined this request")
        if finish == "length":
            raise LLMError("the model's reply was cut off (max tokens)")
        text = (ch.get("message") or {}).get("content") or ""
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"model returned invalid JSON: {e}") from e
        if not isinstance(data, dict):
            raise LLMError("model returned JSON that is not an object")
        u = j.get("usage") or {}
        cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
        return LLMReply(data, j.get("model") or models[0], ms, int(u.get("prompt_tokens") or 0) - cached,
                        int(u.get("completion_tokens") or 0), cached, text)

    def _post(self, body: dict) -> dict:
        import httpx
        headers = {"Authorization": f"Bearer {self.key}", "X-Title": "AI Business Analyst demo"}
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            self.budget.take()
            try:
                r = self.client.post(OPENROUTER_URL, headers=headers, json=body)
            except httpx.HTTPError as e:
                if attempt == self.max_retries:
                    raise LLMError("cannot reach the LLM provider") from e
                self.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 200:
                return r.json()
            try:
                msg = str((r.json().get("error") or {}).get("message", ""))
            except ValueError:
                msg = r.text[:200]
            if r.status_code not in (429, 500, 502, 503, 504) or attempt == self.max_retries:
                kind = {401: "rejected the key", 402: "needs credits", 429: "rate limit or free quota reached"}.get(
                    r.status_code, f"error {r.status_code}")
                raise LLMError(f"LLM provider {kind}: {msg[:200]}")
            wait = r.headers.get("retry-after")
            self.sleep(min(float(wait) if wait and wait.replace(".", "", 1).isdigit() else delay, 30))
            delay *= 2
        raise LLMError("LLM provider unavailable")


class FallbackLLM:
    """Use a remote model, and fall back to the offline rules for a task when the model fails
    (rate limit, used-up free quota, outage). For live demos on free tiers: the answer still comes,
    and its trace shows `offline-rules` as the model. The eval harness switches this off."""

    def __init__(self, primary, offline):
        self.primary, self.offline, self.enabled = primary, offline, True
        self.name = primary.name
        self.fallbacks = 0

    def json(self, task, system, user, schema, tier="fast", context=None) -> LLMReply:
        try:
            return self.primary.json(task, system, user, schema, tier, context)
        except LLMError:
            if not self.enabled or task not in self.offline.handlers:
                raise
            self.fallbacks += 1
            return self.offline.json(task, system, user, schema, tier, context)


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
    """provider: argument > ANALYST_LLM_PROVIDER env > llm.provider in config."""
    provider = provider or os.environ.get("ANALYST_LLM_PROVIDER") or settings["llm"].get("provider", "auto")
    if provider == "auto":
        provider = ("anthropic" if has_anthropic_credentials() else "gemini" if gemini_api_key()
                    else "openrouter" if os.environ.get("OPENROUTER_API_KEY") else "offline")
    remote = {"anthropic": AnthropicLLM, "gemini": GeminiLLM, "openrouter": OpenRouterLLM}.get(provider)
    if remote is not None:
        llm = remote(settings)
        if settings["llm"].get("fallback_to_offline") and offline_handlers:
            return FallbackLLM(llm, OfflineLLM(offline_handlers))
        return llm
    if provider == "offline":
        return OfflineLLM(offline_handlers)
    raise ValueError(f"unknown llm provider {provider!r}")
