"""Provider abstraction + budget/usage accounting.

Any OpenAI-compatible endpoint works (Ollama, vLLM, LiteLLM, hosted APIs); nothing is tied to one vendor.
Every call goes through :class:`AIGateway`, which enforces the monthly budget and a per-user daily call
limit, logs tokens/cost to ``ai_usage`` (never prompts, CV text or secrets) and turns every failure into
:class:`AIUnavailable`, so callers fall back to deterministic logic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

import httpx
from sqlalchemy import func, select

from ..db.models import AiUsage
from ..db.session import Database, utcnow
from ..logging import get_logger

log = get_logger(__name__)


class AIUnavailable(Exception):
    pass


@dataclass
class ChatResult:
    data: dict[str, Any]
    tokens_in: int
    tokens_out: int


class AIProvider(Protocol):
    name: str
    chat_model: str
    embed_model: str

    async def chat_json(self, system: str, user: str, *, max_tokens: int = 800) -> ChatResult: ...

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]: ...


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, chat_model: str, embed_model: str = "", timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None, extra_body: dict | None = None) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport)
        self.chat_model = chat_model
        self.embed_model = embed_model
        self.extra_body = dict(extra_body or {})
        # Reasoning models (OpenAI GPT-5 / o-series) reject ``max_tokens`` and a non-default ``temperature``;
        # they need ``max_completion_tokens`` with room for hidden reasoning. Detected on the first 400, then kept.
        self.reasoning_mode = False

    def _body(self, system: str, user: str, max_tokens: int) -> dict:
        body: dict = {
            "model": self.chat_model,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.reasoning_mode:
            body["max_completion_tokens"] = max_tokens * 4
        else:
            body["temperature"] = 0
            body["max_tokens"] = max_tokens
        body.update(self.extra_body)
        return body

    async def chat_json(self, system: str, user: str, *, max_tokens: int = 800) -> ChatResult:
        r = await self._http.post("/chat/completions", json=self._body(system, user, max_tokens))
        if r.status_code == 400 and not self.reasoning_mode and any(
            w in r.text for w in ("max_tokens", "max_completion_tokens", "temperature", "unsupported_parameter", "unsupported_value")
        ):
            self.reasoning_mode = True
            log.info("ai_provider_reasoning_mode", model=self.chat_model)
            r = await self._http.post("/chat/completions", json=self._body(system, user, max_tokens))
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"].get("content") or ""
        usage = data.get("usage") or {}
        return ChatResult(parse_json_object(content), int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        if not self.embed_model:
            raise AIUnavailable("no embedding model configured")
        r = await self._http.post("/embeddings", json={"model": self.embed_model, "input": texts})
        r.raise_for_status()
        data = r.json()
        vectors = [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
        return vectors, int((data.get("usage") or {}).get("prompt_tokens", 0))


def parse_json_object(content: str) -> dict:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not m:
            raise AIUnavailable("model did not return JSON") from None
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise AIUnavailable("model did not return a JSON object")
    return obj


class AIGateway:
    def __init__(self, db: Database, provider: AIProvider | None, *, label: str = "AI", monthly_budget_usd: float = 5.0,
                 daily_user_calls: int = 30, price_in_per_1k: float = 0.0, price_out_per_1k: float = 0.0) -> None:
        self.db = db
        self.provider = provider
        self.label = label
        self.monthly_budget = monthly_budget_usd
        self.daily_user_calls = daily_user_calls
        self.price_in = price_in_per_1k
        self.price_out = price_out_per_1k
        self.calls = 0  # for tests / demos

    @property
    def available(self) -> bool:
        return self.provider is not None

    @property
    def can_embed(self) -> bool:
        return self.provider is not None and bool(getattr(self.provider, "embed_model", ""))

    async def _check_budget(self, user_id: int | None) -> None:
        async with self.db.session() as s:
            month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            spent = (await s.execute(select(func.coalesce(func.sum(AiUsage.cost_usd), 0.0)).where(AiUsage.created_at >= month_start))).scalar()
            if self.monthly_budget > 0 and spent >= self.monthly_budget:
                raise AIUnavailable("monthly AI budget exhausted")
            if user_id is not None:
                n = (await s.execute(select(func.count()).select_from(AiUsage).where(
                    AiUsage.user_id == user_id, AiUsage.cached.is_(False), AiUsage.created_at >= utcnow() - timedelta(days=1)))).scalar()
                if n >= self.daily_user_calls:
                    raise AIUnavailable("daily AI limit for this user reached")

    async def _log(self, *, user_id: int | None, purpose: str, model: str, tin: int, tout: int, ok: bool, error: str | None = None,
                   cached: bool = False) -> None:
        cost = tin / 1000 * self.price_in + tout / 1000 * self.price_out
        async with self.db.session() as s:
            s.add(AiUsage(user_id=user_id, purpose=purpose, provider=self.provider.name if self.provider else "none", model=model,
                          tokens_in=tin, tokens_out=tout, cost_usd=cost, cached=cached, ok=ok, error_kind=error, created_at=utcnow()))

    async def chat_json(self, system: str, user: str, *, purpose: str, user_id: int | None, max_tokens: int = 800) -> dict:
        if not self.provider:
            raise AIUnavailable("no AI provider configured")
        await self._check_budget(user_id)
        self.calls += 1
        try:
            res = await self.provider.chat_json(system, user, max_tokens=max_tokens)
        except AIUnavailable as exc:
            await self._log(user_id=user_id, purpose=purpose, model=self.provider.chat_model, tin=0, tout=0, ok=False, error="bad_output")
            raise exc
        except Exception as exc:
            await self._log(user_id=user_id, purpose=purpose, model=self.provider.chat_model, tin=0, tout=0, ok=False, error=type(exc).__name__[:40])
            log.warning("ai_call_failed", purpose=purpose, error=type(exc).__name__)
            raise AIUnavailable(type(exc).__name__) from exc
        await self._log(user_id=user_id, purpose=purpose, model=self.provider.chat_model, tin=res.tokens_in, tout=res.tokens_out, ok=True)
        return res.data

    async def embed(self, texts: list[str], *, purpose: str, user_id: int | None) -> list[list[float]]:
        if not self.can_embed:
            raise AIUnavailable("no embedding model configured")
        await self._check_budget(None)
        self.calls += 1
        try:
            vectors, tokens = await self.provider.embed(texts)
        except Exception as exc:
            await self._log(user_id=user_id, purpose=purpose, model=self.provider.embed_model, tin=0, tout=0, ok=False, error=type(exc).__name__[:40])
            raise AIUnavailable(type(exc).__name__) from exc
        await self._log(user_id=user_id, purpose=purpose, model=self.provider.embed_model, tin=tokens, tout=0, ok=True)
        return vectors

    async def log_cache_hit(self, purpose: str, user_id: int | None) -> None:
        await self._log(user_id=user_id, purpose=purpose, model="cache", tin=0, tout=0, ok=True, cached=True)
