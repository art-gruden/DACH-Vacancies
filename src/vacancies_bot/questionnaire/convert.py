"""Questionnaire answers ⇄ :class:`CandidateProfile` / :class:`SearchCriteria`."""

from __future__ import annotations

import re
from typing import Any

from ..domain.criteria import (
    CandidateProfile,
    CityRef,
    LanguageRef,
    NotificationSettings,
    Provenance,
    RoleRef,
    SalaryPref,
    SearchCriteria,
    SkillRef,
)
from .steps import STEPS

_SPLIT = re.compile(r"[;\n]+")
_SPLIT_COMMA = re.compile(r"[,;\n]+")


def _list(text: str | None, comma: bool = True) -> list[str]:
    if not text:
        return []
    return [x.strip() for x in (_SPLIT_COMMA if comma else _SPLIT).split(text) if x.strip()][:30]


def parse_amount(text: str) -> float | None:
    t = text.lower().replace("’", "").replace("'", "").replace(" ", "")
    m = re.match(r"^(\d+(?:[.,]\d+)?)(k|к|т|тыс\.?)?", t)
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    if m.group(2):
        num *= 1000
    elif "." in m.group(1) and len(m.group(1).split(".")[1]) == 3:  # 75.000
        num = float(m.group(1).replace(".", ""))
    return num if 0 < num < 10_000_000 else None


def with_defaults(answers: dict, mode: str) -> dict:
    """Fill steps that the flow did not show with their defaults (``None`` stays unknown)."""
    out = dict(answers)
    for s in STEPS:
        if s.default is not None and out.get(s.key) is None:
            out[s.key] = s.default
    return out


def answers_to_profile(answers: dict, mode: str, version: int = 1) -> CandidateProfile:
    a = with_defaults(answers, mode)
    core = set(a.get("core_skills") or [])
    prof = a.get("proficiency") or {}
    skills = [
        SkillRef(
            name=s["name"], canonical=bool(s.get("canonical", True)), core=s["name"] in core,
            proficiency=prof.get(s["name"]), source=Provenance(s.get("source", Provenance.USER_CONFIRMED)),
        )
        for s in a.get("skills") or []
    ]
    levels = a.get("language_levels") or {}
    languages = [
        LanguageRef(code=code, level=None if levels.get(code) in (None, "unknown") else levels[code]) for code in a.get("languages") or []
    ]
    salary = None
    if a.get("salary_amount") not in (None, ""):
        salary = SalaryPref(
            amount=float(a["salary_amount"]), currency=a.get("salary_currency") or "EUR", period=a.get("salary_period") or "year",
            strict=a.get("salary_strict") == "strict", include_unknown=a.get("salary_include_unknown", "yes") == "yes",
        )
    quiet = a.get("quiet_hours") or "22-08"
    qs, qe = (None, None) if quiet == "none" else (f"{quiet.split('-')[0]}:00", f"{quiet.split('-')[1]}:00")
    criteria = SearchCriteria(
        roles=[RoleRef(**r) for r in a.get("target_roles") or []],
        current_title=a.get("current_title") or None,
        experience=a.get("experience"),
        preferred_seniority=list(a.get("preferred_seniority") or []),
        skills=skills,
        additional_skills_text=a.get("additional_skills_text") or None,
        countries=list(a.get("countries") or ["DE", "AT", "CH"]),
        regions=list(a.get("regions") or []),
        cities=[CityRef(**c) for c in a.get("cities") or []],
        mobility=list(a.get("mobility") or []),
        work_models=list(a.get("work_models") or []),
        remote_from=a.get("remote_from"),
        employment_types=list(a.get("employment_types") or []),
        languages=languages,
        language_flexibility=a.get("language_flexibility") or "WARN",
        industries=list(a.get("industries") or []),
        salary=salary,
        hard_constraints=_list(a.get("hard_constraints"), comma=False),
        soft_preferences=_list(a.get("soft_preferences")),
        exclusions=_list(a.get("exclusions")),
        work_authorization=dict(a.get("work_authorization") or {}),
        search_breadth=a.get("search_breadth") or "BALANCED",
        min_score=int(a.get("min_score") or 70),
    )
    return CandidateProfile(
        profile_version=version,
        ui_language=a.get("ui_language") or "ru",
        criteria=criteria,
        experience_summary=a.get("experience_summary") or None,
        notifications=NotificationSettings(
            mode=a.get("notification_mode") or "IMMEDIATE", timezone=a.get("timezone") or "Europe/Berlin",
            quiet_start=qs, quiet_end=qe, daily_limit=int(a.get("daily_limit") or 5),
        ),
        wants_cv_upload=a.get("cv") == "upload",
    )


def profile_to_answers(p: CandidateProfile) -> dict[str, Any]:
    c = p.criteria
    a: dict[str, Any] = {
        "ui_language": p.ui_language,
        "target_roles": [r.model_dump(mode="json") for r in c.roles],
        "current_title": c.current_title,
        "experience": c.experience,
        "preferred_seniority": c.preferred_seniority,
        "skills": [{"name": s.name, "canonical": s.canonical, "source": s.source.value} for s in c.skills],
        "core_skills": [s.name for s in c.skills if s.core],
        "proficiency": {s.name: s.proficiency for s in c.skills if s.proficiency},
        "additional_skills_text": c.additional_skills_text,
        "countries": c.countries,
        "regions": c.regions,
        "cities": [x.model_dump() for x in c.cities],
        "mobility": c.mobility,
        "work_models": c.work_models,
        "remote_from": c.remote_from,
        "employment_types": c.employment_types,
        "languages": [x.code for x in c.languages],
        "language_levels": {x.code: x.level or "unknown" for x in c.languages},
        "language_flexibility": c.language_flexibility,
        "industries": c.industries,
        "hard_constraints": "\n".join(c.hard_constraints) or None,
        "soft_preferences": ", ".join(c.soft_preferences) or None,
        "exclusions": ", ".join(c.exclusions) or None,
        "experience_summary": p.experience_summary,
        "work_authorization": c.work_authorization,
        "search_breadth": c.search_breadth,
        "min_score": str(c.min_score),
        "notification_mode": p.notifications.mode,
        "timezone": p.notifications.timezone,
        "quiet_hours": "none" if not p.notifications.quiet_start else f"{p.notifications.quiet_start[:2]}-{p.notifications.quiet_end[:2]}",
        "daily_limit": str(p.notifications.daily_limit),
    }
    if c.salary:
        a.update(salary_amount=c.salary.amount, salary_currency=c.salary.currency, salary_period=c.salary.period,
                 salary_strict="strict" if c.salary.strict else "soft", salary_include_unknown="yes" if c.salary.include_unknown else "no")
    return {k: v for k, v in a.items() if v is not None}
