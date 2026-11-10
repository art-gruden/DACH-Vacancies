from conftest import devops_criteria
from vacancies_bot.crawler.models import JobDetail
from vacancies_bot.domain.criteria import LanguageRef, SalaryPref
from vacancies_bot.domain.matching import hard_filter, language_fit, score_job
from vacancies_bot.domain.query_builder import build_queries
from vacancies_bot.testing.fake_crawler import L, S, default_jobs, make_job

JOBS = {j["id"]: JobDetail.model_validate(j) for j in default_jobs()}


def codes(items):
    return {r.code for r in items}


def test_language_fit_semantics():
    cand = {"de": "B2", "en": "C1"}
    assert language_fit(cand, JOBS[1]) == "compatible"
    assert language_fit(cand, JOBS[8]) == "exceeds"  # German C2 required
    assert language_fit(cand, JOBS[10]) == "unknown"  # English required, no level
    assert language_fit(cand, JOBS[12]) == "unknown"  # nothing stated → unknown, never a mismatch
    assert language_fit({"en": "C1"}, JOBS[1]) == "exceeds"  # German required, not spoken
    assert language_fit({"de": None, "en": "C1"}, JOBS[1]) == "unknown"  # candidate gave no level


def test_hard_filter_devops_profile():
    c = devops_criteria()
    passed = {i for i, j in JOBS.items() if hard_filter(j, c).passed}
    assert passed == {1, 2, 6, 9, 10, 14}
    assert codes(hard_filter(JOBS[4], c).failed) >= {"excluded", "employment"}
    assert "work_model" in codes(hard_filter(JOBS[3], c).failed)
    assert "location" in codes(hard_filter(JOBS[5], c).failed)
    assert "closed" in codes(hard_filter(JOBS[13], c).failed)
    assert "specialization" in codes(hard_filter(JOBS[11], c).failed)


def test_remote_path_and_unknowns_are_not_mismatches():
    c = devops_criteria()
    r6 = hard_filter(JOBS[6], c)  # remote, eligibility unknown, location unknown, employment unknown
    assert r6.passed and r6.via_remote
    assert "remote_scope_unknown" in codes(r6.unknown) and "employment_unknown" in codes(r6.unknown)
    r10 = hard_filter(JOBS[10], c)  # located in Graz (AT), but remote allowed for DACH residents
    assert r10.passed and r10.via_remote
    c_at = devops_criteria(remote_from="UNDISCLOSED")
    assert not hard_filter(JOBS[10], c_at).passed  # cannot be verified → only the location path remains


def test_language_flexibility_modes():
    job = JobDetail.model_validate(make_job(50, "SRE", city=1, work_model="HYBRID", employment=["FULL_TIME"], languages=[L("de", "C2")]))
    assert not hard_filter(job, devops_criteria(language_flexibility="NO")).passed
    warn = hard_filter(job, devops_criteria(language_flexibility="WARN"))
    assert warn.passed and "language_exceeds" in codes(warn.warnings)
    assert hard_filter(job, devops_criteria(language_flexibility="YES")).passed


def test_salary_strict_and_unknown():
    job_low = JobDetail.model_validate(make_job(51, "SRE", city=1, work_model="HYBRID", employment=["FULL_TIME"], salary=S(50000, 60000, "EUR")))
    job_none = JobDetail.model_validate(make_job(52, "SRE", city=1, work_model="HYBRID", employment=["FULL_TIME"]))
    job_chf = JobDetail.model_validate(make_job(53, "SRE", city=9, work_model="HYBRID", employment=["FULL_TIME"], salary=S(130000, None, "CHF")))
    strict = devops_criteria(salary=SalaryPref(amount=75000, currency="EUR", strict=True, include_unknown=True))
    assert not hard_filter(job_low, strict).passed
    assert hard_filter(job_none, strict).passed and "salary_unknown" in codes(hard_filter(job_none, strict).unknown)
    assert hard_filter(job_chf, strict).passed  # other currency: never converted → unknown, not a mismatch
    strict_known = devops_criteria(salary=SalaryPref(amount=75000, currency="EUR", strict=True, include_unknown=False))
    assert not hard_filter(job_none, strict_known).passed
    soft = devops_criteria()
    assert hard_filter(job_low, soft).passed and score_job(job_low, soft).components["other"] < 1


def test_score_explanations():
    c = devops_criteria()
    m1 = score_job(JOBS[1], c)
    assert m1.passed and m1.score >= 75
    assert {"spec_match", "skills_match", "location_match", "language_ok"} <= codes(m1.why)
    assert "skills_missing" in codes(m1.gaps)  # Go, Prometheus are in the vacancy but not in the profile
    assert "salary_unknown" in codes(m1.unknowns)  # CHF salary vs EUR wish → unknown, not "below"
    m6 = score_job(JOBS[6], c)
    assert m6.score < m1.score and m6.completeness < m1.completeness
    failed = score_job(JOBS[3], c)
    assert not failed.passed and failed.score <= 30
    assert 0 <= m1.score <= 100 and m1.to_json()["version"]


def test_feedback_multipliers_change_soft_ranking_only():
    c = devops_criteria()
    base = score_job(JOBS[9], c)
    boosted = score_job(JOBS[9], c, multipliers={"role": 2.0})
    assert boosted.score < base.score  # job 9 is an adjacent role; weighting the role higher ranks it lower
    assert boosted.passed == base.passed


def test_query_plan_uses_documented_parameters():
    c = devops_criteria()
    plan = build_queries(c)
    labels = [q.label for q in plan]
    assert labels == ["countries+spec", "countries+skills", "remote+spec", "remote+skills"]
    q0 = dict(plan[0].to_params())
    assert q0["specialization"] == "devops-sre" and "languages" not in q0  # WARN → language fit is local
    remote = plan[2].to_params()
    assert ("remote_in", "DE") in remote and ("work_model", "REMOTE") in remote and ("include_unknown", "remote") in remote
    strict = build_queries(devops_criteria(search_breadth="STRICT", language_flexibility="NO",
                                           salary=SalaryPref(amount=75000, currency="EUR", strict=True)))
    p = dict(strict[0].to_params())
    assert p["languages"] == "de:B2,en:C1" and p["lang_mode"] == "inclusive"
    assert p["salary_min"] == "75000" and p["currency"] == "EUR"
    assert all("skills" not in dict(q.to_params()) for q in strict)


def test_query_plan_free_text_role_and_regions():
    from vacancies_bot.domain.criteria import CityRef, RoleRef
    c = devops_criteria(roles=[RoleRef(name="Platform Engineer", free_text=True)], regions=["DE-SN"],
                        cities=[CityRef(id=1, name="Berlin", country="DE")], remote_from=None, work_models=["ONSITE"],
                        languages=[LanguageRef(code="de", level="C1")])
    plan = build_queries(c)
    assert [q.label for q in plan] == ["regions+title", "regions+skills", "cities+title", "cities+skills"]
    assert dict(plan[0].to_params())["q"] == '"Platform Engineer"'
