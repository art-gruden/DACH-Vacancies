"""Public names for the generated contract models + the enumerations the API accepts.

``models_generated.py`` is produced from ``contracts/crawler-bot-v1.openapi.json`` (see
``scripts/regenerate_client.sh``) and must not be edited by hand. The constants below repeat the values
written in the OpenAPI parameter descriptions; ``tests/test_contract.py`` checks that they still match.
"""

from __future__ import annotations

from .models_generated import (
    CareerCrawlerBotApiJobDetail as JobDetail,
)
from .models_generated import (
    CareerCrawlerBotApiJobOut as JobOut,
)
from .models_generated import (
    CareerCrawlerBotApiJobPage as JobPage,
)
from .models_generated import (
    ChangeItem,
    ChangesPage,
    CityFacet,
    CompanyRef,
    CountryFacet,
    Head,
    LanguageOut,
    LocationOut,
    QualityOut,
    RegionFacet,
    RemoteEligibility,
    SalaryOut,
    SkillFacet,
    SkillOut,
    SpecializationFacet,
    SpecializationOut,
)

# query enumerations (from the parameter descriptions of GET /jobs)
COUNTRIES_DACH = ("DE", "AT", "CH")
WORK_MODELS = ("REMOTE", "HYBRID", "ONSITE")
EMPLOYMENT_TYPES = (
    "FULL_TIME", "PART_TIME", "CONTRACT", "FREELANCE", "TEMPORARY",
    "INTERNSHIP", "APPRENTICESHIP", "WORKING_STUDENT", "OTHER",
)
SENIORITIES = ("INTERN", "JUNIOR", "MID", "SENIOR", "LEAD", "EXECUTIVE")
INCLUDE_UNKNOWN = ("location", "work_model", "employment", "seniority", "specialization", "salary", "remote")
SKILLS_MODES = ("any", "all")
LANG_MODES = ("inclusive", "strict")
SORTS = ("newest", "relevance", "salary_desc")
STATUSES = ("open", "closed", "expired", "all")
CEFR = ("A1", "A2", "B1", "B2", "C1", "C2")
EVENT_TYPES = ("job.created", "job.updated", "job.closed", "job.reopened")

__all__ = [
    "JobDetail", "JobOut", "JobPage", "ChangeItem", "ChangesPage", "CityFacet", "CompanyRef", "CountryFacet",
    "Head", "LanguageOut", "LocationOut", "QualityOut", "RegionFacet", "RemoteEligibility", "SalaryOut",
    "SkillFacet", "SkillOut", "SpecializationFacet", "SpecializationOut",
    "COUNTRIES_DACH", "WORK_MODELS", "EMPLOYMENT_TYPES", "SENIORITIES", "INCLUDE_UNKNOWN", "SKILLS_MODES",
    "LANG_MODES", "SORTS", "STATUSES", "CEFR", "EVENT_TYPES",
]
