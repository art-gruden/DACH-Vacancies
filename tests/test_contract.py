"""The client must use only what the crawler's OpenAPI snapshot declares."""

import json
import re
from pathlib import Path

import pytest

from vacancies_bot.crawler import models as m
from vacancies_bot.crawler.client import JobQuery

SPEC = json.loads((Path(__file__).parents[1] / "contracts" / "crawler-bot-v1.openapi.json").read_text())
JOBS = SPEC["paths"]["/api/bot/v1/jobs"]["get"]
PARAMS = {p["name"]: p for p in JOBS["parameters"]}


def test_nine_bot_routes_present():
    assert set(SPEC["paths"]) == {
        "/api/bot/v1/jobs", "/api/bot/v1/jobs/{job_id}", "/api/bot/v1/facets/countries", "/api/bot/v1/facets/regions",
        "/api/bot/v1/facets/cities", "/api/bot/v1/facets/specializations", "/api/bot/v1/facets/skills",
        "/api/bot/v1/changes", "/api/bot/v1/changes/head",
    }
    assert all(list(v) == ["get"] for v in SPEC["paths"].values()), "bot API is read-only"


def _desc_values(name: str) -> set[str]:
    return set(re.findall(r"\b[A-Z][A-Z_]+\b", PARAMS[name]["schema"].get("description") or PARAMS[name]["description"]))


def test_enum_constants_match_parameter_descriptions():
    assert set(m.WORK_MODELS) == _desc_values("work_model")
    assert set(m.EMPLOYMENT_TYPES) == _desc_values("employment")
    assert set(m.SENIORITIES) == _desc_values("seniority")
    inc = PARAMS["include_unknown"]["description"].split(":", 1)[1]
    assert set(m.INCLUDE_UNKNOWN) == {x.strip() for x in inc.split(",")}
    assert set(m.SKILLS_MODES) == set(PARAMS["skills_mode"]["schema"]["enum"])
    assert set(m.LANG_MODES) == set(PARAMS["lang_mode"]["schema"]["enum"])
    assert set(m.SORTS) == set(PARAMS["sort"]["schema"]["enum"])
    assert set(m.STATUSES) == set(PARAMS["status"]["schema"]["enum"])
    ev = SPEC["components"]["schemas"]["ChangeItem"]["properties"]["event_type"]["enum"]
    assert set(m.EVENT_TYPES) == set(ev)
    lvl = SPEC["components"]["schemas"]["LanguageOut"]["properties"]["level"]["anyOf"][0]["enum"]
    assert tuple(lvl) == m.CEFR


def test_job_query_uses_only_declared_parameters():
    q = JobQuery(
        q="sre", exclude=["Werkstudent"], country=["DE"], region=["DE-SN"], city=[5], specialization=["devops-sre"],
        work_model=["REMOTE"], employment=["FULL_TIME"], seniority=["SENIOR"], skills=["Kubernetes"], skills_mode="all",
        languages={"de": "B2", "en": None}, lang_mode="strict", salary_min=70000, currency="EUR", employer=[1],
        status="all", remote_in=["DE"], include_unknown=["salary"], sort="relevance",
    )
    names = {k for k, _ in q.to_params()} | {"limit", "cursor"}
    assert names <= set(PARAMS), names - set(PARAMS)


@pytest.mark.parametrize("model,schema", [
    (m.JobOut, "career_crawler__bot_api__JobOut"), (m.JobDetail, "career_crawler__bot_api__JobDetail"),
    (m.ChangeItem, "ChangeItem"), (m.ChangesPage, "ChangesPage"), (m.CityFacet, "CityFacet"),
    (m.SpecializationFacet, "SpecializationFacet"), (m.SalaryOut, "SalaryOut"), (m.LanguageOut, "LanguageOut"),
])
def test_generated_models_match_schema(model, schema):
    props = SPEC["components"]["schemas"][schema]["properties"]
    assert set(model.model_fields) == set(props)


def test_bad_values_rejected_locally():
    with pytest.raises(ValueError):
        JobQuery(work_model=["OFFICE"]).validate()
    with pytest.raises(ValueError):
        JobQuery(salary_min=1000).validate()  # currency is mandatory
    with pytest.raises(ValueError):
        JobQuery(languages={"de": "B3"}).validate()
