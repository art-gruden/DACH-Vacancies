"""Initial / manual search: query plan → crawler → merge → local hard filter → explainable ranking.

Deterministic retrieval always works without AI. An optional ``reranker`` (embeddings) only re-scores the
already-filtered top candidates and can never let a job through that failed a hard constraint.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from ..crawler.client import CrawlerClient, CrawlerError, CrawlerValidationError, JobQuery
from ..crawler.models import JobOut
from ..domain.criteria import SearchCriteria
from ..domain.matching import MatchResult, score_job
from ..domain.query_builder import build_queries
from ..logging import get_logger

log = get_logger(__name__)


class Reranker(Protocol):
    async def similarities(self, user_id: int | None, criteria: SearchCriteria, jobs: list[JobOut]) -> dict[int, float]: ...


@dataclass
class Ranked:
    job: JobOut
    match: MatchResult


@dataclass
class SearchOutcome:
    ranked: list[Ranked]  # passed hard constraints, best first
    candidates: int
    queries: list[JobQuery]
    failed_queries: list[str] = field(default_factory=list)
    rejected: int = 0

    def above(self, threshold: int) -> list[Ranked]:
        return [r for r in self.ranked if r.match.score >= threshold]


class SearchService:
    def __init__(self, client: CrawlerClient, *, per_query: int = 50, reranker: Reranker | None = None,
                 semantic_weight: float = 0.2, rerank_top: int = 30) -> None:
        self.client = client
        self.per_query = per_query
        self.reranker = reranker
        self.semantic_weight = semantic_weight
        self.rerank_top = rerank_top

    async def _run_query(self, q: JobQuery) -> list[JobOut]:
        page = await self.client.search_jobs(q, limit=min(self.per_query, 100))
        return page.items

    async def search(self, c: SearchCriteria, *, user_id: int | None = None, multipliers: dict | None = None) -> SearchOutcome:
        queries = build_queries(c)
        results = await asyncio.gather(*(self._run_query(q) for q in queries), return_exceptions=True)
        jobs: dict[int, JobOut] = {}
        failed: list[str] = []
        errors: list[BaseException] = []
        for q, res in zip(queries, results, strict=True):
            if isinstance(res, CrawlerValidationError) and q.skills:
                # an unknown skill name: retry this part without the skill filter rather than failing everything
                log.warning("skills_rejected_by_crawler", label=q.label)
                failed.append(q.label)
                continue
            if isinstance(res, BaseException):
                failed.append(q.label)
                errors.append(res)
                continue
            for j in res:
                jobs.setdefault(j.id, j)
        if errors and not jobs:
            err = errors[0]
            raise err if isinstance(err, CrawlerError) else CrawlerError(str(err))

        scored = [Ranked(j, score_job(j, c, multipliers=multipliers)) for j in jobs.values()]
        passed = [r for r in scored if r.match.passed]
        rejected = len(scored) - len(passed)
        passed.sort(key=lambda r: (r.match.score, r.job.first_seen_at), reverse=True)

        if self.reranker and passed:
            top = passed[: self.rerank_top]
            try:
                sims = await self.reranker.similarities(user_id, c, [r.job for r in top])
            except Exception as exc:  # AI must never break search
                log.warning("rerank_failed", error=type(exc).__name__)
                sims = {}
            if sims:
                for r in top:
                    if r.job.id in sims:
                        r.match = score_job(r.job, c, multipliers=multipliers, semantic=sims[r.job.id],
                                            semantic_weight=self.semantic_weight)
                passed.sort(key=lambda r: (r.match.score, r.job.first_seen_at), reverse=True)
        return SearchOutcome(ranked=passed, candidates=len(jobs), queries=queries, failed_queries=failed, rejected=rejected)
