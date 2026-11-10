"""Async client for the crawler's read-only Bot API (``/api/bot/v1``).

Contract: ``contracts/crawler-bot-v1.openapi.json``. Authentication is ``Authorization: Bearer <token>``
(the crawler's ``gcc_core.botauth``). Error mapping follows the crawler documentation:

* 401 → :class:`CrawlerAuthError` (wrong token), 503 → :class:`CrawlerDisabled` (no key configured or misconfigured)
* 422 → :class:`CrawlerValidationError` (unknown skill / country / foreign cursor …), never retried
* 429 → waits ``Retry-After`` (capped) and retries; afterwards :class:`CrawlerRateLimited`
* ``/changes``: 400 → :class:`InvalidCursorError`, 409 → :class:`CursorAheadError` (re-sync from ``/changes/head``)
* network errors and other 5xx → exponential backoff, then :class:`CrawlerUnavailable`

The token is never logged.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, TypeAdapter

from ..logging import get_logger
from .models import (
    CEFR,
    COUNTRIES_DACH,
    EMPLOYMENT_TYPES,
    INCLUDE_UNKNOWN,
    LANG_MODES,
    SENIORITIES,
    SKILLS_MODES,
    SORTS,
    STATUSES,
    WORK_MODELS,
    ChangesPage,
    CityFacet,
    CountryFacet,
    Head,
    JobDetail,
    JobOut,
    JobPage,
    RegionFacet,
    SkillFacet,
    SpecializationFacet,
)

log = get_logger(__name__)
M = TypeVar("M", bound=BaseModel)


class CrawlerError(Exception):
    status: int | None = None

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CrawlerAuthError(CrawlerError):
    pass


class CrawlerDisabled(CrawlerError):
    pass


class CrawlerValidationError(CrawlerError):
    def __init__(self, message: str, detail: Any = None) -> None:
        super().__init__(message, 422)
        self.detail = detail


class CrawlerRateLimited(CrawlerError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after:.0f}s", 429)
        self.retry_after = retry_after


class InvalidCursorError(CrawlerError):
    pass


class CursorAheadError(CrawlerError):
    pass


class CrawlerUnavailable(CrawlerError):
    pass


@dataclass
class JobQuery:
    """Parameters of ``GET /jobs`` exactly as the API names them. Validation mirrors the documented enums so
    a mistake is caught before a request (the server still has the last word, 422)."""

    q: str | None = None
    exclude: list[str] = field(default_factory=list)
    country: list[str] = field(default_factory=list)
    region: list[str] = field(default_factory=list)
    city: list[int] = field(default_factory=list)
    specialization: list[str] = field(default_factory=list)
    work_model: list[str] = field(default_factory=list)
    employment: list[str] = field(default_factory=list)
    seniority: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    skills_mode: str = "any"
    languages: dict[str, str | None] = field(default_factory=dict)  # {"de": "B2", "en": None}
    lang_mode: str = "inclusive"
    salary_min: float | None = None
    currency: str | None = None
    salary_period: str = "year"
    new_since: datetime | None = None
    employer: list[int] = field(default_factory=list)
    status: str = "open"
    remote_in: list[str] = field(default_factory=list)
    include_unknown: list[str] = field(default_factory=list)
    sort: str = "newest"
    label: str = ""  # local only: which part of the query plan produced this query

    def validate(self) -> None:
        def check(values: Sequence[str], allowed: Sequence[str], name: str) -> None:
            bad = [v for v in values if v not in allowed]
            if bad:
                raise ValueError(f"{name}: unsupported value(s) {bad}")

        check(self.work_model, WORK_MODELS, "work_model")
        check(self.employment, EMPLOYMENT_TYPES, "employment")
        check(self.seniority, SENIORITIES, "seniority")
        check(self.include_unknown, INCLUDE_UNKNOWN, "include_unknown")
        check([self.skills_mode], SKILLS_MODES, "skills_mode")
        check([self.lang_mode], LANG_MODES, "lang_mode")
        check([self.sort], SORTS, "sort")
        check([self.status], STATUSES, "status")
        for c in [*self.country, *self.remote_in]:
            if len(c) != 2 or not c.isupper():
                raise ValueError(f"country code {c!r} must be ISO 3166-1 alpha-2")
        for lvl in self.languages.values():
            if lvl is not None and lvl not in CEFR:
                raise ValueError(f"CEFR level {lvl!r}")
        if self.salary_min is not None and not self.currency:
            raise ValueError("salary_min requires currency")
        if self.sort == "salary_desc" and not self.currency:
            raise ValueError("sort=salary_desc requires currency")
        if self.q and len(self.q) > 200:
            raise ValueError("q is limited to 200 characters")

    def to_params(self) -> list[tuple[str, str]]:
        self.validate()
        p: list[tuple[str, str]] = []

        def many(name: str, values: Sequence[Any]) -> None:
            for v in values:
                p.append((name, str(v)))

        if self.q:
            p.append(("q", self.q))
        many("exclude", self.exclude)
        many("country", self.country)
        many("region", self.region)
        many("city", self.city)
        many("specialization", self.specialization)
        many("work_model", self.work_model)
        many("employment", self.employment)
        many("seniority", self.seniority)
        many("skills", self.skills)
        if self.skills:
            p.append(("skills_mode", self.skills_mode))
        if self.languages:
            p.append(("languages", ",".join(f"{k}:{v}" if v else k for k, v in self.languages.items())))
            p.append(("lang_mode", self.lang_mode))
        if self.salary_min is not None:
            p.append(("salary_min", f"{self.salary_min:g}"))
        if self.currency:
            p.append(("currency", self.currency))
            p.append(("salary_period", self.salary_period))
        if self.new_since:
            p.append(("new_since", self.new_since.isoformat()))
        many("employer", self.employer)
        if self.status != "open":
            p.append(("status", self.status))
        many("remote_in", self.remote_in)
        many("include_unknown", self.include_unknown)
        p.append(("sort", self.sort))
        return p


_countries = TypeAdapter(list[CountryFacet])
_regions = TypeAdapter(list[RegionFacet])
_cities = TypeAdapter(list[CityFacet])
_specs = TypeAdapter(list[SpecializationFacet])
_skills = TypeAdapter(list[SkillFacet])


class CrawlerClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 20.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retry_after: float = 60.0,
        sleep=asyncio.sleep,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "vacancies-bot/2.0"},
            timeout=timeout,
            transport=transport,
        )
        self.max_retries = max_retries
        self.max_retry_after = max_retry_after
        self._sleep = sleep
        self.calls = 0  # for tests / metrics

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- transport ----------------------------------------------------------------------------------

    async def _get(self, path: str, params: Any = None, *, feed: bool = False) -> Any:
        attempt = 0
        while True:
            attempt += 1
            self.calls += 1
            try:
                resp = await self._http.get(path, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt > self.max_retries:
                    raise CrawlerUnavailable(f"{path}: {type(exc).__name__}") from exc
                await self._sleep(self._backoff(attempt))
                continue
            code = resp.status_code
            if code == 200:
                return resp.json()
            if code == 404:
                return None
            detail = _detail(resp)
            if code == 401:
                raise CrawlerAuthError("crawler rejected the API token (check CRAWLER_API_TOKEN)", 401)
            if code == 422:
                raise CrawlerValidationError(f"{path}: {detail}", detail)
            if feed and code == 400:
                raise InvalidCursorError(str(detail), 400)
            if feed and code == 409:
                raise CursorAheadError(str(detail), 409)
            if code == 429:
                retry_after = _retry_after(resp)
                if attempt > self.max_retries or retry_after > self.max_retry_after:
                    raise CrawlerRateLimited(retry_after)
                log.info("crawler_rate_limited", path=path, retry_after=retry_after)
                await self._sleep(retry_after)
                continue
            if code == 503 and any(w in str(detail).lower() for w in ("disabled", "not configured", "misconfigured")):
                raise CrawlerDisabled(f"crawler Bot API: {detail}", 503)
            if code >= 500:
                if attempt > self.max_retries:
                    raise CrawlerUnavailable(f"{path}: HTTP {code}", code)
                await self._sleep(self._backoff(attempt))
                continue
            raise CrawlerError(f"{path}: HTTP {code} {detail}", code)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(30.0, 0.5 * 2 ** (attempt - 1)) * (0.8 + 0.4 * random.random())

    # --- endpoints ----------------------------------------------------------------------------------

    async def search_jobs(self, query: JobQuery, *, limit: int = 25, cursor: str | None = None) -> JobPage:
        params = query.to_params() + [("limit", str(max(1, min(limit, 100))))]
        if cursor:
            params.append(("cursor", cursor))
        return JobPage.model_validate(await self._get("/jobs", params))

    async def iter_jobs(self, query: JobQuery, *, max_items: int = 100, page_size: int = 50) -> AsyncIterator[JobOut]:
        cursor: str | None = None
        seen = 0
        while seen < max_items:
            page = await self.search_jobs(query, limit=min(page_size, max_items - seen), cursor=cursor)
            for item in page.items:
                yield item
                seen += 1
            if not page.has_more or not page.next_cursor:
                return
            cursor = page.next_cursor

    async def get_job(self, job_id: int) -> JobDetail | None:
        data = await self._get(f"/jobs/{int(job_id)}")
        return JobDetail.model_validate(data) if data is not None else None

    async def countries(self) -> list[CountryFacet]:
        return _countries.validate_python(await self._get("/facets/countries"))

    async def regions(self, country: str) -> list[RegionFacet]:
        return _regions.validate_python(await self._get("/facets/regions", {"country": country}))

    async def cities(self, country: str, region: str | None = None, q: str | None = None, limit: int = 100) -> list[CityFacet]:
        params: dict[str, Any] = {"country": country, "limit": max(1, min(limit, 500))}
        if region:
            params["region"] = region
        if q:
            params["q"] = q[:60]
        return _cities.validate_python(await self._get("/facets/cities", params))

    async def specializations(self) -> list[SpecializationFacet]:
        return _specs.validate_python(await self._get("/facets/specializations"))

    async def skills(self, q: str | None = None, limit: int = 50) -> list[SkillFacet]:
        params: dict[str, Any] = {"limit": max(1, min(limit, 200))}
        if q:
            params["q"] = q[:40]
        return _skills.validate_python(await self._get("/facets/skills", params))

    async def changes(self, cursor: str | None, limit: int = 100) -> ChangesPage:
        params: dict[str, Any] = {"limit": max(1, min(limit, 500))}
        if cursor:
            params["cursor"] = cursor
        return ChangesPage.model_validate(await self._get("/changes", params, feed=True))

    async def changes_head(self) -> str:
        return Head.model_validate(await self._get("/changes/head", feed=True)).cursor


def _detail(resp: httpx.Response) -> Any:
    try:
        body = resp.json()
        return body.get("detail", body) if isinstance(body, dict) else body
    except Exception:
        return resp.text[:300]


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(1.0, float(resp.headers.get("Retry-After", "5")))
    except ValueError:
        return 5.0


__all__ = [
    "CrawlerClient", "JobQuery", "CrawlerError", "CrawlerAuthError", "CrawlerDisabled", "CrawlerValidationError",
    "CrawlerRateLimited", "InvalidCursorError", "CursorAheadError", "CrawlerUnavailable", "COUNTRIES_DACH",
]
