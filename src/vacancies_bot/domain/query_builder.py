"""Turns confirmed criteria into a small plan of ``GET /jobs`` queries.

Only parameters that the crawler documents are used. The crawler AND-combines filters, so "location OR
remote from my country" and "my specialization OR my core skills" become separate queries whose results are
merged and then filtered/ranked locally with :func:`domain.matching.score_job`. Things the API cannot filter
(industries, mobility, free-text constraints, company/title exclusions) are applied locally only.

Mapping (criteria → API):

| criteria | API parameter | hard? |
|---|---|---|
| countries / regions / cities | ``country`` / ``region`` / ``city`` (one query per level) | yes, unknown location only in BROAD |
| work_models | ``work_model`` + ``include_unknown=work_model`` | yes (unknown kept) |
| remote_from (+REMOTE) | separate query ``work_model=REMOTE&remote_in=..&include_unknown=remote`` | yes |
| employment_types | ``employment`` + ``include_unknown=employment`` | yes (unknown kept) |
| languages, flexibility NO | ``languages=de:B2,en:C1&lang_mode=inclusive`` | yes (unknown kept) |
| languages, WARN/YES | not sent; fit computed locally to keep "exceeds" with a warning | no |
| salary strict | ``salary_min``+``currency``+``salary_period`` (+``include_unknown=salary``) | yes |
| salary soft | not sent; ranking only | no |
| roles | ``specialization`` (leaf/group slugs); free-text roles → ``q`` | by breadth |
| core skills (BALANCED/BROAD) | extra query ``skills=..&skills_mode=any`` | no (adjacent roles) |
| preferred seniority | not sent (title-only signal in the crawler; ranking) | no |
| exclusions | local title/company match | yes |
"""

from __future__ import annotations

from ..crawler.client import JobQuery
from .criteria import SearchCriteria

MAX_QUERIES = 6


def _base(c: SearchCriteria) -> JobQuery:
    q = JobQuery(sort="relevance")
    if c.work_models and set(c.work_models) != {"REMOTE", "HYBRID", "ONSITE"}:
        q.work_model = list(c.work_models)
        q.include_unknown.append("work_model")
    if c.employment_types:
        q.employment = list(c.employment_types)
        q.include_unknown.append("employment")
    if c.languages and c.language_flexibility == "NO":
        q.languages = c.language_map()
        q.lang_mode = "inclusive"
    if c.salary and c.salary.strict:
        q.salary_min = c.salary.amount
        q.currency = c.salary.currency
        q.salary_period = c.salary.period
        if c.salary.include_unknown:
            q.include_unknown.append("salary")
    return q


def _clone(src: JobQuery, **changes) -> JobQuery:
    data = {k: (list(v) if isinstance(v, list) else (dict(v) if isinstance(v, dict) else v)) for k, v in src.__dict__.items()}
    data.update(changes)
    return JobQuery(**data)


def _location_variants(c: SearchCriteria, base: JobQuery) -> list[JobQuery]:
    out: list[JobQuery] = []
    if c.regions or c.cities:
        if c.regions:
            out.append(_clone(base, region=list(c.regions), label="regions"))
        if c.cities:
            out.append(_clone(base, city=[x.id for x in c.cities], label="cities"))
    elif c.countries:
        out.append(_clone(base, country=list(c.countries), label="countries"))
    else:
        out.append(_clone(base, label="anywhere"))
    if c.search_breadth == "BROAD":
        for q in out:
            q.include_unknown.append("location")
    home = (c.remote_from or "").upper()
    if c.wants_remote() and home in ("DE", "AT", "CH"):
        out.append(
            _clone(
                base,
                work_model=["REMOTE"],
                remote_in=[home],
                include_unknown=[x for x in base.include_unknown if x != "work_model"] + ["remote"],
                label="remote",
            )
        )
    return out


def build_queries(c: SearchCriteria) -> list[JobQuery]:
    base = _base(c)
    role_variants: list[dict] = []
    slugs = c.role_slugs()
    free = c.free_text_roles()
    if slugs:
        role_variants.append({"specialization": slugs, "label": "spec"})
    if free:
        text = " or ".join(f'"{t}"' if " " in t else t for t in free)[:200]
        role_variants.append({"q": text, "label": "title"})
    core = c.canonical_skill_names(core_only=True)
    if (c.search_breadth in ("BALANCED", "BROAD") or not role_variants) and core:
        role_variants.append({"skills": core[:8], "skills_mode": "any", "label": "skills"})
    if c.search_breadth == "BROAD":
        groups = c.role_groups()
        if groups and set(groups) != set(slugs):
            role_variants.append({"specialization": groups, "label": "group"})
    if not role_variants:
        role_variants.append({"label": "all"})

    plan: list[JobQuery] = []
    for loc in _location_variants(c, base):
        for rv in role_variants:
            label = f"{loc.label}+{rv.get('label')}"
            extra = {k: v for k, v in rv.items() if k != "label"}
            q = _clone(loc, label=label, **extra)
            q.validate()
            plan.append(q)
    # dedupe identical parameter sets, keep order, cap the plan
    uniq: list[JobQuery] = []
    seen: set[tuple] = set()
    for q in plan:
        key = tuple(q.to_params())
        if key not in seen:
            seen.add(key)
            uniq.append(q)
    return uniq[:MAX_QUERIES]
