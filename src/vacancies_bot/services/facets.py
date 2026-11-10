"""Reference data for the questionnaire comes from the crawler's ``/facets/*`` (the source of truth).

Two cache levels: process memory and the ``facet_cache`` table. When the crawler is unreachable, stale data is
served (``stale=True``) instead of failing the questionnaire; with no data at all, the caller offers free text.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter

from ..crawler.client import CrawlerClient, CrawlerError
from ..crawler.models import CityFacet, CountryFacet, RegionFacet, SkillFacet, SpecializationFacet
from ..db.models import FacetCache
from ..db.session import Database, as_aware, utcnow
from ..logging import get_logger

log = get_logger(__name__)


@dataclass
class SpecEntry:
    id: int
    slug: str
    name: str
    name_de: str
    group: str | None  # parent group slug; None for a group itself
    is_group: bool
    open_jobs: int


class FacetsService:
    def __init__(self, client: CrawlerClient, db: Database, ttl_s: int = 900) -> None:
        self.client = client
        self.db = db
        self.ttl = ttl_s
        self._mem: dict[str, tuple[float, list]] = {}
        self.stale = False

    async def _cached(self, key: str, loader, adapter: TypeAdapter) -> list[Any]:
        now = time.monotonic()
        hit = self._mem.get(key)
        if hit and now - hit[0] < self.ttl:
            return adapter.validate_python(hit[1])
        async with self.db.session() as s:
            row = await s.get(FacetCache, key)
            if row and (utcnow() - as_aware(row.fetched_at)).total_seconds() < self.ttl:
                self._mem[key] = (now, row.payload)
                return adapter.validate_python(row.payload)
        try:
            items = await loader()
            payload = [x.model_dump(mode="json") for x in items]
        except CrawlerError as exc:
            log.warning("facets_unavailable", key=key, error=type(exc).__name__)
            self.stale = True
            if row is not None:
                return adapter.validate_python(row.payload)
            if hit:
                return adapter.validate_python(hit[1])
            return []
        self.stale = False
        self._mem[key] = (now, payload)
        async with self.db.session() as s:
            stmt = self.db.insert(FacetCache).values(key=key, payload=payload, fetched_at=utcnow())
            await s.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"payload": payload, "fetched_at": utcnow()}))
        return items

    async def countries(self) -> list[CountryFacet]:
        return await self._cached("countries", self.client.countries, TypeAdapter(list[CountryFacet]))

    async def regions(self, country: str) -> list[RegionFacet]:
        return await self._cached(f"regions:{country}", lambda: self.client.regions(country), TypeAdapter(list[RegionFacet]))

    async def cities(self, country: str, q: str | None = None, limit: int = 30) -> list[CityFacet]:
        key = f"cities:{country}:{(q or '').casefold()[:40]}:{limit}"
        return await self._cached(key, lambda: self.client.cities(country, q=q, limit=limit), TypeAdapter(list[CityFacet]))

    async def specializations(self) -> list[SpecializationFacet]:
        return await self._cached("specializations", self.client.specializations, TypeAdapter(list[SpecializationFacet]))

    async def spec_index(self) -> dict[str, SpecEntry]:
        out: dict[str, SpecEntry] = {}
        for g in await self.specializations():
            out[g.slug] = SpecEntry(g.id, g.slug, g.name, g.name_de, None, True, g.open_jobs)
            for c in g.children or []:
                out[c.slug] = SpecEntry(c.id, c.slug, c.name, c.name_de, g.slug, False, c.open_jobs)
        return out

    async def skills_top(self, limit: int = 30) -> list[SkillFacet]:
        return await self._cached(f"skills::{limit}", lambda: self.client.skills(limit=limit), TypeAdapter(list[SkillFacet]))

    async def skills_search(self, q: str, limit: int = 20) -> list[SkillFacet]:
        q = q.strip()[:40]
        return await self._cached(f"skills:{q.casefold()}:{limit}", lambda: self.client.skills(q=q, limit=limit), TypeAdapter(list[SkillFacet]))

    async def canonical_skill(self, name: str) -> str | None:
        """The crawler's canonical spelling of a skill, or None when the crawler does not know it."""
        name = name.strip()
        if not name:
            return None
        for item in await self.skills_search(name[:40], limit=50):
            if item.name.casefold() == name.casefold():
                return item.name
        return None

    async def skill_vocabulary(self, limit: int = 200) -> list[str]:
        return [x.name for x in await self._cached(f"skills::{limit}", lambda: self.client.skills(limit=limit), TypeAdapter(list[SkillFacet]))]
