import io

import pytest
from sqlalchemy import func, select

from conftest import devops_criteria
from vacancies_bot.ai.cv import CvError, CvStore, scrub_pii
from vacancies_bot.ai.explain import Explainer, build_facts
from vacancies_bot.ai.profile_parser import ProfileParser, apply_facts
from vacancies_bot.ai.provider import AIGateway, AIUnavailable, OpenAICompatProvider
from vacancies_bot.ai.search_assistant import SearchAssistant
from vacancies_bot.ai.semantic import SemanticReranker
from vacancies_bot.crawler.models import JobDetail
from vacancies_bot.db.models import AiUsage, BotUser, CvDocument
from vacancies_bot.domain.matching import score_job
from vacancies_bot.services.search import SearchService
from vacancies_bot.testing.fake_ai import FakeAIProvider
from vacancies_bot.testing.fake_crawler import default_jobs

TEXT = ("SRE / DevOps, 6 лет опыта: Kubernetes, Terraform, Ansible, GitLab CI. Немецкий B2, английский C1. "
        "Ищу remote из Германии. Не присылать Werkstudent.")


def kinds(facts):
    return {(f.kind, str(f.value.get("name") or f.value.get("code") or f.value.get("value") or f.value.get("bucket"))) for f in facts}


@pytest.fixture
async def gw(db):
    async with db.session() as s:
        s.add(BotUser(id=1, chat_id=1, ai_consent_version=1))
    return AIGateway(db, FakeAIProvider(), label="Fake AI", monthly_budget_usd=1.0, daily_user_calls=50,
                     price_in_per_1k=0.001, price_out_per_1k=0.002)


async def test_rule_parser_without_ai(facets):
    res = await ProfileParser(facets, None).parse(TEXT, user_id=1, use_ai=False)
    k = kinds(res.facts)
    assert {("skill", "Kubernetes"), ("skill", "Terraform"), ("skill", "Ansible"), ("skill", "GitLab CI")} <= k
    assert ("role", "DevOps / SRE") in k and ("experience", "6-10") in k
    assert ("language", "de") in k and ("language", "en") in k
    assert next(f for f in res.facts if f.kind == "language" and f.value["code"] == "de").value["level"] == "B2"
    assert ("work_model", "REMOTE") in k and ("remote_from", "DE") in k and ("exclusion", "Werkstudent") in k
    assert not res.used_ai
    assert all(f.evidence.casefold() in TEXT.casefold() for f in res.facts)


async def test_ai_parser_drops_invented_facts(facets, gw):
    text = "SRE with Kubernetes and Helm. German is fine. English C1. I have been in ops for a while."
    res = await ProfileParser(facets, gw).parse(text, user_id=1, use_ai=True)
    assert res.used_ai and gw.provider.chat_calls == 1
    names = {f.value.get("name") for f in res.facts if f.kind == "skill"}
    assert "Rust" not in names and {"Kubernetes", "Helm"} <= names  # invented skill dropped
    de = next(f for f in res.facts if f.kind == "language" and f.value["code"] == "de")
    assert de.value["level"] is None  # "C1" was never written next to German
    en = next(f for f in res.facts if f.kind == "language" and f.value["code"] == "en")
    assert en.value["level"] == "C1"
    assert not any(f.kind == "experience" for f in res.facts)  # "experienced" has no number → dropped
    role = next(f for f in res.facts if f.kind == "role" and f.by == "ai")
    assert role.value.get("free_text")  # not in the crawler taxonomy → free text, never a fake slug
    assert res.question


async def test_confirmed_facts_only_are_applied(facets):
    res = await ProfileParser(facets, None).parse(TEXT, user_id=1, use_ai=False)
    chosen = [f for f in res.facts if f.kind in ("skill", "language")]
    answers, n = apply_facts({"ui_language": "ru"}, chosen)
    assert n == len(chosen)
    assert {s["name"] for s in answers["skills"]} == {"Kubernetes", "Terraform", "Ansible", "GitLab CI"}
    assert all(s["source"] == "user_confirmed" for s in answers["skills"])
    assert "experience" not in answers and "target_roles" not in answers


async def test_budget_and_provider_failures_fall_back(db, facets, gw):
    gw.provider.fail = True
    res = await ProfileParser(facets, gw).parse(TEXT, user_id=1, use_ai=True)
    assert not res.used_ai and res.ai_error and res.facts  # rules still work
    gw.provider.fail = False
    gw.monthly_budget = 0.0000001
    async with db.session() as s:
        s.add(AiUsage(user_id=None, purpose="x", provider="fake", model="m", cost_usd=1.0))
    with pytest.raises(AIUnavailable):
        await gw.chat_json("s", "u", purpose="test", user_id=1)
    async with db.session() as s:
        rows = (await s.execute(select(AiUsage.purpose, AiUsage.ok))).all()
    assert ("profile_parse_text", False) in rows


async def test_assistant_respects_consent_and_overrides_profile(facets, gw):
    from vacancies_bot.domain.criteria import CandidateProfile

    base = CandidateProfile(criteria=devops_criteria(countries=["CH"], work_models=["ONSITE"]))
    crit, res = await SearchAssistant(ProfileParser(facets, gw)).interpret(
        "SRE remote из Германии, Terraform, без Werkstudent", base, user_id=1, use_ai=False)
    assert gw.provider.chat_calls == 0  # no consent → no AI call
    assert crit.work_models == ["REMOTE"] and crit.remote_from == "DE" and "DE" in crit.countries
    assert "Werkstudent" in crit.exclusions
    assert crit.language_map() == {"de": "B2", "en": "C1"}  # untouched parts come from the profile


async def test_explainer_keeps_only_grounded_points(gw):
    job = JobDetail.model_validate(default_jobs()[0])
    c = devops_criteria()
    m = score_job(job, c).to_json()
    out = await Explainer(gw).explain(job, m, c, "ru", user_id=1)
    assert out and len(out["points"]) == 1
    assert "150000" not in str(out) and "визу" not in str(out)
    facts = build_facts(job, m, c)
    assert any("Vacancy salary" in f for f in facts) and not any("@" in f for f in facts)


async def test_semantic_rerank_is_cached_and_needs_consent(db, client, gw):
    rr = SemanticReranker(db, gw)
    svc = SearchService(client, reranker=rr, semantic_weight=0.2)
    c = devops_criteria()
    out1 = await svc.search(c, user_id=1)
    first = gw.provider.embedded_texts
    out2 = await svc.search(c, user_id=1)
    assert first > 0 and gw.provider.embedded_texts == first  # second search: everything from the cache
    assert [r.job.id for r in out1.ranked] == [r.job.id for r in out2.ranked]
    assert all(r.match.semantic is not None for r in out1.ranked)
    async with db.session() as s:
        s.add(BotUser(id=2, chat_id=2))
    out3 = await svc.search(c, user_id=2)  # no consent → no semantic component
    assert all(r.match.semantic is None for r in out3.ranked)


def _docx(lines: list[str]) -> bytes:
    from docx import Document

    d = Document()
    for line in lines:
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


async def test_cv_pipeline_scrubs_and_deletes(db, facets, gw):
    from cryptography.fernet import Fernet

    data = _docx([
        "Max Mustermann", "Senior DevOps Engineer", "E-Mail: max@example.com", "Telefon: +49 170 1234567",
        "Musterstraße 12, 01067 Dresden", "Geburtsdatum: 01.02.1990", "Nationalität: deutsch",
        "8 Jahre Erfahrung mit Kubernetes, Terraform und Ansible. https://linkedin.com/in/max", "Englisch C1, Deutsch C2",
    ])
    store = CvStore(db, Fernet.generate_key().decode(), retention_hours=24)
    text, sha = store.prepare(data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "cv.docx")
    for secret in ("max@example.com", "1234567", "Musterstraße", "01.02.1990", "Mustermann", "linkedin", "Nationalität"):
        assert secret not in text, secret
    assert "Kubernetes" in text and "8 Jahre" in text
    res = await ProfileParser(facets, gw).parse(text, user_id=1, use_ai=True, source="cv")
    assert {f.source for f in res.facts} == {"cv"}
    assert ("experience", "6-10") in kinds(res.facts)
    doc_id = await store.remember(1, sha, "docx", len(data), text, res.to_json())
    async with db.session() as s:
        doc = await s.get(CvDocument, doc_id)
        assert b"Kubernetes" not in doc.encrypted_text and store.decrypt(doc) == text
    assert await store.wipe(1, confirmed=True) == 1
    async with db.session() as s:
        doc = await s.get(CvDocument, doc_id)
        assert doc.encrypted_text is None and doc.extracted is None and doc.status == "confirmed"
    with pytest.raises(CvError):
        store.prepare(b"%PDF-1.4 garbage", "application/pdf", "x.pdf")
    with pytest.raises(CvError):
        CvStore(db, "", max_bytes=10).prepare(data, "docx", "cv.docx")


def test_scrub_keeps_professional_content():
    out = scrub_pii("Kubernetes since 2019\nTel. 0351 1234567\nTerraform, Go\nwww.example.com")
    assert "Kubernetes since 2019" in out and "Terraform, Go" in out and "1234567" not in out and "example.com" not in out


async def test_openai_compat_provider_wire_format(db):
    import httpx

    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        body = __import__("json").loads(req.content)
        seen["body"] = body
        if req.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n{\"ok\": true}\n```"}}],
                                             "usage": {"prompt_tokens": 12, "completion_tokens": 3}})
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}],
                                         "usage": {"prompt_tokens": 4}})

    p = OpenAICompatProvider("http://llm.local/v1", "k", "m", "e", transport=httpx.MockTransport(handler))
    gw = AIGateway(db, p, price_in_per_1k=1.0, price_out_per_1k=2.0)
    assert await gw.chat_json("sys", "user", purpose="t", user_id=None) == {"ok": True}
    assert seen["auth"] == "Bearer k" and seen["body"]["temperature"] == 0 and seen["body"]["response_format"] == {"type": "json_object"}
    assert await gw.embed(["a", "b"], purpose="e", user_id=None) == [[1, 0], [0, 1]]
    async with db.session() as s:
        cost = (await s.execute(select(func.sum(AiUsage.cost_usd)))).scalar()
    assert cost == pytest.approx(12 / 1000 + 3 / 1000 * 2 + 4 / 1000)


async def test_provider_switches_to_reasoning_parameters(db):
    import json as _json

    import httpx

    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = _json.loads(req.content)
        bodies.append(body)
        if "max_tokens" in body:  # what api.openai.com answers for GPT-5 / o-series models
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens' is not supported with this model. "
                                                                  "Use 'max_completion_tokens' instead.", "code": "unsupported_parameter"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": 1}'}}], "usage": {}})

    p = OpenAICompatProvider("https://api.openai.com/v1", "k", "gpt-x", transport=httpx.MockTransport(handler),
                             extra_body={"reasoning_effort": "low"})
    gw = AIGateway(db, p)
    assert await gw.chat_json("s", "u", purpose="t", user_id=None, max_tokens=100) == {"ok": 1}
    assert await gw.chat_json("s", "u", purpose="t", user_id=None, max_tokens=100) == {"ok": 1}
    assert len(bodies) == 3  # one rejected request, then the mode is remembered
    assert bodies[-1]["max_completion_tokens"] == 400 and "temperature" not in bodies[-1] and bodies[-1]["reasoning_effort"] == "low"
