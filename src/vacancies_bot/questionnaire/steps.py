"""The 33 questions of the specification (section 2.2) as data.

``modes``: which flows show the step — ``quick`` (2–3 minutes), ``extended`` (AI-assisted full profile),
``manual`` (filters for a one-off search, no subscription). Steps not shown in a flow take ``default``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Q, E, M = "quick", "extended", "manual"
ALL = {Q, E}


@dataclass(frozen=True)
class Step:
    key: str
    num: str  # number in the specification
    kind: str  # single | multi | text | spec | skills | regions | cities | per_item | confirm | info
    modes: frozenset[str]
    required: bool = False
    options: tuple[str, ...] = ()
    any_option: bool = False  # adds "any" (stored as [])
    condition: Callable[[dict], bool] | None = None
    default: Any = None
    max_select: int | None = None
    items_from: str | None = None  # per_item: the answer key whose values are iterated
    item_options: tuple[str, ...] = ()
    optional_text: bool = False  # single/multi steps that also accept a typed value
    meta: dict = field(default_factory=dict)


def _has(key: str) -> Callable[[dict], bool]:
    return lambda a: bool(a.get(key))


def _remote(a: dict) -> bool:
    wm = a.get("work_models")
    return wm == [] or (wm is not None and "REMOTE" in wm)


def _salary(a: dict) -> bool:
    return a.get("salary_amount") not in (None, "")


def _experienced(a: dict) -> bool:
    return a.get("experience") not in ("0", "<1")


STEPS: list[Step] = [
    Step("ui_language", "01", "single", frozenset(ALL), True, ("ru", "uk", "de", "en")),
    Step("target_roles", "02", "spec", frozenset({Q, E, M}), True),
    Step("current_title", "03", "text", frozenset({E})),
    Step("experience", "04", "single", frozenset(ALL), True, ("0", "<1", "1-2", "3-5", "6-10", "10+", "unknown")),
    Step("preferred_seniority", "05", "multi", frozenset({E}), False, ("INTERN", "JUNIOR", "MID", "SENIOR", "LEAD"), any_option=True, default=[]),
    Step("skills", "06", "skills", frozenset({Q, E, M}), True, meta={"required_if": "experienced"}),
    Step("core_skills", "07", "multi", frozenset({E}), False, condition=_has("skills"), max_select=5),
    Step("proficiency", "08", "per_item", frozenset({E}), False, condition=_has("core_skills"), items_from="core_skills",
         item_options=("learning", "basic", "working", "advanced", "expert")),
    Step("additional_skills_text", "09", "text", frozenset({E})),
    Step("countries", "10", "multi", frozenset({Q, E, M}), True, ("DE", "AT", "CH"), default=["DE", "AT", "CH"]),
    Step("regions", "11", "regions", frozenset({E}), False, default=[]),
    Step("cities", "12", "cities", frozenset({E}), False, default=[]),
    Step("mobility", "13", "multi", frozenset({E}), False, ("none", "relocation", "commute", "business_travel"), default=[]),
    Step("work_models", "14", "multi", frozenset({Q, E, M}), True, ("REMOTE", "HYBRID", "ONSITE"), any_option=True, default=[]),
    Step("remote_from", "15", "single", frozenset({Q, E}), False, ("DE", "AT", "CH", "OTHER", "UNDISCLOSED"), condition=_remote),
    Step("employment_types", "16", "multi", frozenset(ALL), True,
         ("FULL_TIME", "PART_TIME", "FREELANCE", "CONTRACT", "INTERNSHIP", "APPRENTICESHIP", "WORKING_STUDENT"), any_option=True, default=[]),
    Step("languages", "17", "multi", frozenset(ALL), True, ("de", "en", "fr", "it", "uk", "ru", "pl", "es"), any_option=False,
         optional_text=True, default=[], meta={"none_option": True}),
    Step("language_levels", "18", "per_item", frozenset(ALL), True, condition=_has("languages"), items_from="languages",
         item_options=("A1", "A2", "B1", "B2", "C1", "C2", "unknown")),
    Step("language_flexibility", "19", "single", frozenset(ALL), True, ("NO", "WARN", "YES"), condition=_has("languages"), default="WARN"),
    Step("industries", "20", "multi", frozenset({E}), False, meta={"from": "spec_groups"}, any_option=True, default=[]),
    Step("salary_amount", "21", "text", frozenset({E})),
    Step("salary_currency", "21b", "single", frozenset({E}), True, ("EUR", "CHF"), condition=_salary, default="EUR"),
    Step("salary_period", "21c", "single", frozenset({E}), True, ("year", "month", "hour"), condition=_salary, default="year"),
    Step("salary_strict", "22", "single", frozenset({E}), True, ("strict", "soft"), condition=_salary, default="soft"),
    Step("salary_include_unknown", "22b", "single", frozenset({E}), True, ("yes", "no"), condition=_salary, default="yes"),
    Step("hard_constraints", "23", "text", frozenset({E})),
    Step("soft_preferences", "24", "text", frozenset({E})),
    Step("exclusions", "25", "text", frozenset({E})),
    Step("experience_summary", "26", "text", frozenset({E}), meta={"min": 0, "max": 1500}),
    Step("cv", "27", "single", frozenset({E}), False, ("upload", "later")),
    Step("work_authorization", "28", "per_item", frozenset({E}), False, items_from="countries",
         item_options=("has", "needs_sponsorship", "unknown", "undisclosed")),
    Step("search_breadth", "29", "single", frozenset({E}), True, ("STRICT", "BALANCED", "BROAD"), default="BALANCED"),
    Step("min_score", "30", "single", frozenset({Q, E}), True, ("85", "70", "0"), default="70"),
    Step("notification_mode", "31", "single", frozenset({Q, E}), True, ("IMMEDIATE", "MORNING", "EVENING", "MANUAL"), default="IMMEDIATE"),
    Step("timezone", "32a", "single", frozenset({E}), False, ("Europe/Berlin", "Europe/Vienna", "Europe/Zurich", "Europe/Kyiv"),
         optional_text=True, default="Europe/Berlin"),
    Step("quiet_hours", "32b", "single", frozenset({E}), False, ("22-08", "23-07", "none"), default="22-08"),
    Step("daily_limit", "32c", "single", frozenset({E}), False, ("3", "5", "10", "20"), default="5"),
    Step("confirm", "33", "confirm", frozenset({Q, E, M})),
]

BY_KEY = {s.key: s for s in STEPS}


def flow(mode: str) -> list[Step]:
    return [s for s in STEPS if mode in s.modes]


def is_required(step: Step, answers: dict) -> bool:
    if step.meta.get("required_if") == "experienced":
        return _experienced(answers)
    return step.required


def applicable(step: Step, answers: dict) -> bool:
    return step.condition is None or step.condition(answers)
