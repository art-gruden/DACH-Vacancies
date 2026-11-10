"""Optional semantic reranking with embeddings (cached, only for already-filtered top candidates).

Vacancy vectors are cached per content hash (``job:<id>:<hash>``), profile vectors per text hash, so a vacancy
is embedded once no matter how many users see it. Cosine similarity is mapped to [0, 1]; the scorer mixes it in
with ``AI_SEMANTIC_WEIGHT`` and it can never override a hard constraint.
"""

from __future__ import annotations

import hashlib
import math

from ..crawler.models import JobOut
from ..db.models import BotUser, EmbeddingCache
from ..db.session import Database, utcnow
from ..domain.criteria import SearchCriteria
from .provider import AIGateway, AIUnavailable


def job_text(job: JobOut) -> str:
    parts = [job.title]
    if job.specialization:
        parts.append(job.specialization.name)
    parts.append(", ".join(s.name for s in job.skills))
    desc = getattr(job, "description", None)
    if desc:
        parts.append(desc[:1500])
    return "\n".join(p for p in parts if p)


def profile_text(c: SearchCriteria, summary: str | None = None) -> str:
    parts = [", ".join(r.name for r in c.roles), c.current_title or "", ", ".join(s.name for s in c.skills),
             c.additional_skills_text or "", summary or ""]
    return "\n".join(p for p in parts if p)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class SemanticReranker:
    def __init__(self, db: Database, gateway: AIGateway, *, require_consent: bool = True) -> None:
        self.db = db
        self.gateway = gateway
        self.require_consent = require_consent

    async def _vectors(self, items: dict[str, str], user_id: int | None, purpose: str) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        async with self.db.session() as s:
            for key in items:
                row = await s.get(EmbeddingCache, key)
                if row:
                    out[key] = row.vector
        missing = [k for k in items if k not in out]
        if len(out) and not missing:
            await self.gateway.log_cache_hit(purpose, user_id)
        if missing:
            vectors = await self.gateway.embed([items[k] for k in missing], purpose=purpose, user_id=user_id)
            async with self.db.session() as s:
                for k, v in zip(missing, vectors, strict=True):
                    out[k] = v
                    stmt = self.db.insert(EmbeddingCache).values(key=k, model=self.gateway.provider.embed_model, vector=v, created_at=utcnow())
                    await s.execute(stmt.on_conflict_do_nothing(index_elements=["key"]))
        return out

    async def similarities(self, user_id: int | None, criteria: SearchCriteria, jobs: list[JobOut]) -> dict[int, float]:
        if not self.gateway.can_embed or not jobs:
            return {}
        if self.require_consent and user_id is not None:
            async with self.db.session() as s:
                u = await s.get(BotUser, user_id)
                if not u or not u.ai_consent_version:
                    return {}
        ptext = profile_text(criteria)
        if not ptext.strip():
            return {}
        try:
            pv = await self._vectors({f"profile:{_h(ptext)}": ptext}, user_id, "embed_profile")
            texts = {f"job:{j.id}:{_h(job_text(j))}": job_text(j) for j in jobs}
            jv = await self._vectors(texts, None, "embed_jobs")
        except AIUnavailable:
            return {}
        p = next(iter(pv.values()))
        out = {}
        for j in jobs:
            v = jv.get(f"job:{j.id}:{_h(job_text(j))}")
            if v:
                out[j.id] = max(0.0, min(1.0, (cosine(p, v) - 0.2) / 0.6))
        return out
