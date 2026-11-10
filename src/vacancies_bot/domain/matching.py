"""Deterministic matching: hard constraints first, then an explainable 0–100 relevance score.

The score is a heuristic relevance of the vacancy to the *stated criteria*, not a probability of being
hired. Unknown vacancy data is never treated as a mismatch: it lowers confidence (``completeness``) and is
listed in ``unknowns``. The same functions are used for the initial search (post-filter of API results)
and for the change feed, so a subscription behaves the same in both paths.

Reasons are returned as codes + parameters and localised by :mod:`vacancies_bot.bot.texts`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..crawler.models import JobOut
from .criteria import (
    CEFR_RANK,
    EXPECTED_SENIORITY,
    SENIORITY_ORDER,
    SearchCriteria,
)

DEFAULT_WEIGHTS: dict[str, float] = {"role": 25, "skills": 25, "experience": 15, "geo": 15, "language": 10, "other": 10}
SCORER_VERSION = "rules-1"
PROFICIENCY_FACTOR = {None: 0.8, "learning": 0.4, "basic": 0.6, "working": 0.85, "advanced": 1.0, "expert": 1.0}
DACH = {"DE", "AT", "CH"}
EU_DACH = {"DE", "AT"}  # CH is in Europe but not in the EU


@dataclass
class Reason:
    code: str
    params: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "params": self.params}


@dataclass
class HardResult:
    passed: bool
    failed: list[Reason] = field(default_factory=list)
    unknown: list[Reason] = field(default_factory=list)
    warnings: list[Reason] = field(default_factory=list)
    via_remote: bool = False


@dataclass
class MatchResult:
    job_id: int
    score: int
    passed: bool
    components: dict[str, float]
    why: list[Reason]
    gaps: list[Reason]
    unknowns: list[Reason]
    checks: list[Reason]
    failed: list[Reason]
    completeness: float
    language_fit: str | None
    semantic: float | None = None
    version: str = SCORER_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "passed": self.passed,
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "why": [r.as_dict() for r in self.why],
            "gaps": [r.as_dict() for r in self.gaps],
            "unknowns": [r.as_dict() for r in self.unknowns],
            "checks": [r.as_dict() for r in self.checks],
            "failed": [r.as_dict() for r in self.failed],
            "completeness": round(self.completeness, 2),
            "language_fit": self.language_fit,
            "semantic": self.semantic,
            "version": self.version,
        }


# --- language fit (same semantics as the crawler's ``language_fit``) -------------------------------------


def language_fit(candidate: dict[str, str | None], job: JobOut) -> str:
    """compatible | unknown | exceeds for the vacancy's *required* (or unclear) languages.

    * a required language the candidate does not speak, or a stated CEFR level above the candidate's → exceeds
    * no level stated, the candidate gave no level, or an unclear requirement → at most unknown
    * a vacancy without any stated requirement → unknown
    """
    verdicts: list[int] = []
    for lang in job.languages:
        if lang.requirement not in ("required", "unknown"):
            continue
        if lang.language not in candidate:
            v = 2
        else:
            cand = candidate[lang.language]
            if cand is None or lang.level is None:
                v = 1
            else:
                v = 2 if CEFR_RANK[lang.level] > CEFR_RANK[cand] else 0
        if lang.requirement == "unknown":
            v = min(v, 1)
        verdicts.append(v)
    if not verdicts:
        return "unknown"
    return {0: "compatible", 1: "unknown", 2: "exceeds"}[max(verdicts)]


# --- helpers ---------------------------------------------------------------------------------------------

_WORD = re.compile(r"[\w+#.]+", re.UNICODE)


def _norm(s: str | None) -> str:
    return (s or "").casefold()


def contains_term(text: str | None, term: str) -> bool:
    """Case-insensitive whole-word(ish) containment; multi-word terms match as phrases."""
    t = _norm(term).strip()
    if not t:
        return False
    return re.search(r"(?<![\w])" + re.escape(t) + r"(?![\w])", _norm(text)) is not None


def _job_text(job: JobOut) -> str:
    parts = [job.title, job.company.name, " ".join(s.name for s in job.skills)]
    desc = getattr(job, "description", None)
    if desc:
        parts.append(desc[:20000])
    for attr in ("requirements", "responsibilities"):
        parts.extend(getattr(job, attr, None) or [])
    return "\n".join(parts)


def _spec_slugs(job: JobOut) -> tuple[str | None, set[str], set[str]]:
    primary = job.specialization.slug if job.specialization else None
    secondary = {s.slug for s in (job.secondary_specializations or [])}
    groups = {s.group for s in [job.specialization, *(job.secondary_specializations or [])] if s and s.group}
    return primary, secondary, groups


def _title_matches_role(job: JobOut, c: SearchCriteria) -> str | None:
    names = [r.name for r in c.roles] + ([c.current_title] if c.current_title else [])
    for n in names:
        if n and contains_term(job.title, n):
            return n
    return None


def _remote_eligible(job: JobOut, c: SearchCriteria) -> tuple[bool, bool]:
    """(eligible, unknown) for a remote employee living in ``c.remote_from``."""
    if job.work_model not in ("REMOTE", "HYBRID") or not c.wants_remote():
        return False, False
    if job.work_model == "HYBRID":  # hybrid still needs presence → only via the location path
        return False, False
    home = (c.remote_from or "").upper()
    scope = job.remote_eligibility.scope
    countries = set(job.remote_eligibility.countries)
    if scope == "unknown":
        return True, True
    if scope == "worldwide":
        return True, False
    if home not in DACH:  # OTHER / UNDISCLOSED: cannot be checked against a country list
        return scope in ("europe", "eu"), True
    if countries:  # the crawler lists the DACH countries a remote employee may live in
        return home in countries, False
    if scope in ("europe", "dach"):
        return True, False
    if scope == "eu":
        return home in EU_DACH, False
    return False, False


def _location_match(job: JobOut, c: SearchCriteria) -> str | None:
    """'city' | 'region' | 'country' when a work location is acceptable, else None."""
    city_ids = {x.id for x in c.cities}
    best = None
    for loc in job.locations:
        if loc.id in city_ids:
            return "city"
        if c.regions and loc.region in c.regions:
            best = "region"
        elif not c.regions and not city_ids and loc.country in c.countries and best is None:
            best = "country"
    return best


# --- hard constraints --------------------------------------------------------------------------------------


def hard_filter(job: JobOut, c: SearchCriteria) -> HardResult:
    r = HardResult(passed=True)
    if job.status != "open":
        r.failed.append(Reason("closed", {"status": job.status}))

    for kw in c.exclusions:
        if contains_term(job.title, kw) or contains_term(job.company.name, kw):
            r.failed.append(Reason("excluded", {"keyword": kw}))
            break

    # location: an acceptable work location, or remote eligibility from the candidate's country
    loc = _location_match(job, c)
    remote_ok, remote_unknown = _remote_eligible(job, c)
    if loc is None:
        if remote_ok:
            r.via_remote = True
            if remote_unknown:
                r.unknown.append(Reason("remote_scope_unknown"))
        elif not job.locations:
            r.unknown.append(Reason("location_unknown"))
        else:
            r.failed.append(Reason("location", {"where": _loc_label(job)}))

    if c.work_models:
        if job.work_model == "UNKNOWN":
            r.unknown.append(Reason("work_model_unknown"))
        elif job.work_model not in c.work_models:
            r.failed.append(Reason("work_model", {"value": job.work_model}))

    if c.employment_types:
        kinds = [k for k in job.employment_types if k != "UNKNOWN"]
        if not kinds:
            r.unknown.append(Reason("employment_unknown"))
        elif not set(kinds) & set(c.employment_types):
            r.failed.append(Reason("employment", {"value": kinds}))

    if c.languages:
        fit = language_fit(c.language_map(), job)
        if fit == "exceeds":
            if c.language_flexibility == "NO":
                r.failed.append(Reason("language_exceeds"))
            elif c.language_flexibility == "WARN":
                r.warnings.append(Reason("language_exceeds"))

    if c.salary and c.salary.strict:
        s = job.salary
        comparable = s is not None and s.currency == c.salary.currency and s.period == c.salary.period
        value = (s.max if s and s.max is not None else (s.min if s else None)) if comparable else None
        if value is None:
            if c.salary.include_unknown:
                r.unknown.append(Reason("salary_unknown"))
            else:
                r.failed.append(Reason("salary_unknown_strict"))
        elif value < c.salary.amount:
            r.failed.append(Reason("salary_below", {"value": value, "currency": s.currency}))

    # specialization: how wide the user wants to search
    if c.roles and c.search_breadth != "BROAD":
        primary, secondary, groups = _spec_slugs(job)
        slugs = set(c.role_slugs())
        title_hit = _title_matches_role(job, c)
        spec_hit = bool(slugs & ({primary} | secondary | groups))
        if not spec_hit and not title_hit:
            if c.search_breadth == "STRICT":
                if primary is None:
                    r.unknown.append(Reason("specialization_unknown"))
                else:
                    r.failed.append(Reason("specialization", {"value": job.specialization.name if job.specialization else None}))
            else:  # BALANCED: adjacent roles allowed when the core skills overlap or the group matches
                core = {_norm(s.name) for s in c.core_skills()}
                overlap = core & {_norm(s.name) for s in job.skills}
                same_group = bool(set(c.role_groups()) & groups)
                if not overlap and not same_group:
                    if primary is None:
                        r.unknown.append(Reason("specialization_unknown"))
                    else:
                        r.failed.append(Reason("specialization", {"value": job.specialization.name if job.specialization else None}))

    r.passed = not r.failed
    return r


def _loc_label(job: JobOut) -> str:
    labels = []
    for loc in job.locations[:3]:
        labels.append(", ".join(x for x in (loc.city, loc.region_name or loc.region, loc.country) if x))
    return "; ".join(labels)


# --- scoring -----------------------------------------------------------------------------------------------


def score_job(
    job: JobOut,
    c: SearchCriteria,
    *,
    weights: dict[str, float] | None = None,
    multipliers: dict[str, float] | None = None,
    semantic: float | None = None,
    semantic_weight: float = 0.0,
) -> MatchResult:
    hard = hard_filter(job, c)
    why: list[Reason] = []
    gaps: list[Reason] = []
    unknowns: list[Reason] = list(hard.unknown)
    checks: list[Reason] = list(hard.warnings)
    comp: dict[str, float] = {}

    # role / tasks -------------------------------------------------------------------------------------
    primary, secondary, groups = _spec_slugs(job)
    slugs = set(c.role_slugs())
    role = 0.1 if c.roles else 0.6
    if c.roles:
        if primary and primary in slugs:
            role = 1.0
            why.append(Reason("spec_match", {"name": job.specialization.name}))
        elif slugs & secondary:
            role = 0.8
            why.append(Reason("spec_secondary", {"name": next(s.name for s in job.secondary_specializations or [] if s.slug in slugs)}))
        elif slugs & groups or set(c.role_groups()) & groups:
            role = 0.5
            why.append(Reason("spec_group", {"name": job.specialization.name if job.specialization else ""}))
        elif primary is None:
            role = 0.4
            unknowns.append(Reason("specialization_unknown"))
        hit = _title_matches_role(job, c)
        if hit and role < 0.8:
            role = max(role, 0.8)
            why.append(Reason("title_match", {"term": hit}))
    comp["role"] = role

    # skills -----------------------------------------------------------------------------------------------
    job_sk = {_norm(s.name): s.name for s in job.skills}
    prof = {_norm(s.name): s for s in c.skills}
    extra = {_norm(w) for w in _WORD.findall(c.additional_skills_text or "") if len(w) > 1}
    if not job_sk:
        skills = 0.5 if c.skills else 0.6
        unknowns.append(Reason("skills_unknown"))
    elif not c.skills:
        skills = 0.3
    else:
        weighted = 0.0
        matched: list[str] = []
        for k, name in job_sk.items():
            if k in prof:
                weighted += PROFICIENCY_FACTOR.get(prof[k].proficiency, 0.8)
                matched.append(name)
            elif k in extra:
                weighted += 0.5
                matched.append(name)
        coverage = weighted / min(len(job_sk), 10)
        core = {_norm(s.name) for s in c.core_skills()}
        core_hit = len(core & set(job_sk)) / len(core) if core else coverage
        skills = min(1.0, 0.65 * min(1.0, coverage) + 0.35 * core_hit)
        if matched:
            why.append(Reason("skills_match", {"skills": matched[:8]}))
        missing = [name for k, name in job_sk.items() if k not in prof and k not in extra]
        if missing:
            gaps.append(Reason("skills_missing", {"skills": missing[:6]}))
    comp["skills"] = skills

    # experience / seniority --------------------------------------------------------------------------------
    wanted = c.preferred_seniority or EXPECTED_SENIORITY.get(c.experience or "", [])
    if job.seniority == "UNKNOWN":
        exp = 0.6
        unknowns.append(Reason("seniority_unknown"))
    elif not wanted:
        exp = 0.7
    elif job.seniority in wanted:
        exp = 1.0
        why.append(Reason("seniority_match", {"value": job.seniority}))
    else:
        jidx = SENIORITY_ORDER.index(job.seniority)
        dist = min(abs(jidx - SENIORITY_ORDER.index(w)) for w in wanted)
        exp = 0.5 if dist == 1 else 0.15
        if jidx > max(SENIORITY_ORDER.index(w) for w in wanted):
            gaps.append(Reason("seniority_higher", {"value": job.seniority}))
        else:
            checks.append(Reason("seniority_lower", {"value": job.seniority}))
    comp["experience"] = exp

    # geography + work model ---------------------------------------------------------------------------------
    loc = _location_match(job, c)
    if loc == "city" or loc == "region" or loc == "country":
        g_loc = 1.0
        why.append(Reason("location_match", {"where": _loc_label(job)}))
    elif hard.via_remote:
        g_loc = 0.6 if any(u.code == "remote_scope_unknown" for u in hard.unknown) else 1.0
        why.append(Reason("remote_eligible", {"from": c.remote_from}))
    elif not job.locations:
        g_loc = 0.5
    else:
        g_loc = 0.0
    if not c.work_models:
        wm = 1.0
    elif job.work_model == "UNKNOWN":
        wm = 0.6
    elif job.work_model in c.work_models:
        wm = 1.0
        why.append(Reason("work_model_match", {"value": job.work_model}))
    else:
        wm = 0.0
    if job.geo_status == "ambiguous":
        checks.append(Reason("geo_ambiguous"))
    comp["geo"] = 0.6 * g_loc + 0.4 * wm

    # languages ------------------------------------------------------------------------------------------------
    fit = language_fit(c.language_map(), job) if c.languages else None
    if fit is None:
        lang = 0.6
    elif fit == "compatible":
        lang = 1.0
        why.append(Reason("language_ok", {"langs": _job_langs(job)}))
    elif fit == "unknown":
        lang = 0.6
        langs = _job_langs(job)
        unknowns.append(Reason("language_unknown", {"langs": langs}) if langs else Reason("language_not_stated"))
    else:
        lang = 0.4 if c.language_flexibility == "YES" else 0.15
        gaps.append(Reason("language_exceeds", {"langs": _job_langs(job)}))
    comp["language"] = lang

    # employment, salary, industry, soft preferences -------------------------------------------------------------
    parts: list[float] = []
    kinds = [k for k in job.employment_types if k != "UNKNOWN"]
    if c.employment_types:
        if not kinds:
            parts.append(0.6)
        elif set(kinds) & set(c.employment_types):
            parts.append(1.0)
        else:
            parts.append(0.0)
    if c.salary:
        s = job.salary
        comparable = s is not None and s.currency == c.salary.currency and s.period == c.salary.period
        value = (s.max if s.max is not None else s.min) if comparable and s else None
        if value is None:
            parts.append(0.6)
            if not any(u.code.startswith("salary") for u in unknowns):
                unknowns.append(Reason("salary_unknown"))
        elif value >= c.salary.amount:
            parts.append(1.0)
            why.append(Reason("salary_ok", {"raw": s.raw or f"{value:g} {s.currency}"}))
        else:
            parts.append(0.2)
            gaps.append(Reason("salary_below", {"value": value, "currency": s.currency}))
    elif job.salary is None:
        unknowns.append(Reason("salary_not_stated"))
    if c.industries:
        parts.append(1.0 if set(c.industries) & groups else (0.5 if not groups else 0.3))
    if c.soft_preferences:
        text = _job_text(job)
        hits = [p for p in c.soft_preferences if contains_term(text, p)]
        parts.append(min(1.0, 0.5 + 0.5 * len(hits) / len(c.soft_preferences)))
        if hits:
            why.append(Reason("soft_pref", {"terms": hits[:5]}))
    comp["other"] = sum(parts) / len(parts) if parts else 0.8

    for hc in c.hard_constraints:
        checks.append(Reason("check_constraint", {"text": hc}))
    if "relocation" not in c.mobility and c.mobility and job.work_model == "ONSITE" and loc is None:
        checks.append(Reason("relocation_needed"))

    w = dict(weights or DEFAULT_WEIGHTS)
    for k, m in (multipliers or {}).items():
        if k in w:
            w[k] = w[k] * max(0.5, min(2.0, m))
    total_w = sum(w.values()) or 1.0
    rule_score = sum(w[k] * comp.get(k, 0) for k in w) / total_w
    final = rule_score
    if semantic is not None and semantic_weight > 0:
        final = (1 - semantic_weight) * rule_score + semantic_weight * semantic
    if not hard.passed:
        final = min(final, 0.3)

    completeness = _completeness(job, unknowns)
    return MatchResult(
        job_id=job.id,
        score=int(round(100 * final)),
        passed=hard.passed,
        components=comp,
        why=why,
        gaps=gaps,
        unknowns=_dedupe(unknowns),
        checks=_dedupe(checks),
        failed=hard.failed,
        completeness=completeness,
        language_fit=fit,
        semantic=semantic,
    )


def _job_langs(job: JobOut) -> list[str]:
    out = []
    for lang in job.languages:
        if lang.requirement == "preferred":
            continue
        out.append(f"{lang.language.upper()} {lang.level or lang.descriptor or '?'}")
    return out


def _completeness(job: JobOut, unknowns: list[Reason]) -> float:
    if job.quality and job.quality.completeness is not None:
        base = float(job.quality.completeness)
    else:
        base = 1.0
    return max(0.0, min(1.0, base - 0.03 * len(unknowns)))


def _dedupe(items: list[Reason]) -> list[Reason]:
    seen: set[tuple] = set()
    out = []
    for r in items:
        key = (r.code, repr(sorted(r.params.items())))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out
