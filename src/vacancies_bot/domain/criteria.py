"""Internal profile / subscription model of the bot.

Every value that maps onto the crawler uses the crawler's own vocabulary (ISO codes, the enums of
``/jobs``, facet slugs and ids, canonical skill names), so the query builder needs no guessing. Values the
crawler cannot filter on (industries, mobility, work authorization, free-text constraints) are kept for
ranking, display and AI explanations only and are documented as such.

``None`` means unknown / not answered; ``[]`` means "answered, nothing selected" (usually: no restriction).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Provenance(StrEnum):
    USER_CONFIRMED = "user_confirmed"
    CV_UNCONFIRMED = "cv_extracted_unconfirmed"
    TEXT_UNCONFIRMED = "text_extracted_unconfirmed"


Proficiency = Literal["learning", "basic", "working", "advanced", "expert"]
CEFRLevel = Literal["A1", "A2", "B1", "B2", "C1", "C2"]
ExperienceBucket = Literal["0", "<1", "1-2", "3-5", "6-10", "10+", "unknown"]
Breadth = Literal["STRICT", "BALANCED", "BROAD"]
LanguageFlex = Literal["NO", "WARN", "YES"]
NotificationMode = Literal["IMMEDIATE", "MORNING", "EVENING", "MANUAL"]

EXPERIENCE_YEARS: dict[str, float | None] = {
    "0": 0, "<1": 0.5, "1-2": 1.5, "3-5": 4, "6-10": 8, "10+": 12, "unknown": None,
}
# what a candidate with that much experience is usually hired as (used only when no seniority was chosen)
EXPECTED_SENIORITY: dict[str, list[str]] = {
    "0": ["INTERN", "JUNIOR"], "<1": ["INTERN", "JUNIOR"], "1-2": ["JUNIOR", "MID"], "3-5": ["MID", "SENIOR"],
    "6-10": ["SENIOR", "LEAD"], "10+": ["SENIOR", "LEAD", "EXECUTIVE"],
}
SENIORITY_ORDER = ["INTERN", "JUNIOR", "MID", "SENIOR", "LEAD", "EXECUTIVE"]
CEFR_RANK = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6}


class RoleRef(BaseModel):
    name: str
    slug: str | None = None  # leaf or group slug from /facets/specializations
    id: int | None = None
    group: str | None = None  # parent group slug (for a leaf)
    is_group: bool = False  # the whole group was chosen; the crawler expands a group slug to its members
    free_text: bool = False  # typed by the user, not in the taxonomy → used as text query only
    source: Provenance = Provenance.USER_CONFIRMED


class SkillRef(BaseModel):
    name: str
    canonical: bool = True  # known to /facets/skills → may be sent as ``skills`` (unknown names give 422)
    proficiency: Proficiency | None = None
    core: bool = False
    source: Provenance = Provenance.USER_CONFIRMED


class LanguageRef(BaseModel):
    code: str  # ISO 639-1
    level: CEFRLevel | None = None  # None = "I don't know my exact level"
    source: Provenance = Provenance.USER_CONFIRMED


class CityRef(BaseModel):
    id: int  # location id from /facets/cities
    name: str
    country: str
    region: str | None = None


class SalaryPref(BaseModel):
    amount: float
    currency: Literal["EUR", "CHF"]
    period: Literal["year", "month", "hour"] = "year"
    strict: bool = False
    include_unknown: bool = True


class SearchCriteria(BaseModel):
    roles: list[RoleRef] = Field(default_factory=list)
    current_title: str | None = None
    experience: ExperienceBucket | None = None
    preferred_seniority: list[str] = Field(default_factory=list)  # crawler enum; [] = any
    skills: list[SkillRef] = Field(default_factory=list)
    additional_skills_text: str | None = None
    countries: list[str] = Field(default_factory=lambda: ["DE"])
    regions: list[str] = Field(default_factory=list)  # ISO 3166-2
    cities: list[CityRef] = Field(default_factory=list)
    mobility: list[str] = Field(default_factory=list)  # none | relocation | commute | business_travel
    work_models: list[str] = Field(default_factory=list)  # REMOTE/HYBRID/ONSITE; [] = any
    remote_from: str | None = None  # DE/AT/CH/OTHER/UNDISCLOSED
    employment_types: list[str] = Field(default_factory=list)  # crawler enum; [] = any
    languages: list[LanguageRef] = Field(default_factory=list)
    language_flexibility: LanguageFlex = "WARN"
    industries: list[str] = Field(default_factory=list)  # specialization group slugs (soft only)
    salary: SalaryPref | None = None
    hard_constraints: list[str] = Field(default_factory=list)  # free text, shown as "check manually"
    soft_preferences: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)  # title / company keywords
    work_authorization: dict[str, str] = Field(default_factory=dict)  # country → has|needs_sponsorship|unknown|undisclosed
    search_breadth: Breadth = "BALANCED"
    min_score: int = 70

    # --- helpers ------------------------------------------------------------------------------------
    def role_slugs(self) -> list[str]:
        return [r.slug for r in self.roles if r.slug and not r.free_text]

    def role_groups(self) -> list[str]:
        out: list[str] = []
        for r in self.roles:
            g = r.slug if r.is_group else r.group
            if g and g not in out:
                out.append(g)
        return out

    def free_text_roles(self) -> list[str]:
        return [r.name for r in self.roles if r.free_text]

    def core_skills(self) -> list[SkillRef]:
        core = [s for s in self.skills if s.core]
        return core or self.skills[:5]

    def canonical_skill_names(self, core_only: bool = False) -> list[str]:
        src = self.core_skills() if core_only else self.skills
        return [s.name for s in src if s.canonical]

    def language_map(self) -> dict[str, str | None]:
        return {lang.code: lang.level for lang in self.languages}

    def wants_remote(self) -> bool:
        return not self.work_models or "REMOTE" in self.work_models


class NotificationSettings(BaseModel):
    mode: NotificationMode = "IMMEDIATE"
    timezone: str = "Europe/Berlin"
    quiet_start: str | None = "22:00"
    quiet_end: str | None = "08:00"
    daily_limit: int = 5


class CandidateProfile(BaseModel):
    """What the questionnaire produces. ``criteria`` is the default for new subscriptions."""

    profile_version: int = 1
    ui_language: Literal["ru", "uk", "de", "en"] = "ru"
    criteria: SearchCriteria = Field(default_factory=SearchCriteria)
    experience_summary: str | None = None
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    wants_cv_upload: bool = False
    confirmed_at: datetime | None = None
