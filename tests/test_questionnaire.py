import json

from vacancies_bot.bot.texts import t
from vacancies_bot.questionnaire.convert import profile_to_answers
from vacancies_bot.questionnaire.engine import Engine, result_profile


def opt_index(d, value):
    return next(i for i, (v, _) in enumerate(d["opts"]) if v == value)


class Driver:
    """Drives the engine like Telegram would, persisting the draft as JSON between updates."""

    def __init__(self, engine, draft):
        self.e = engine
        self.raw = json.dumps(draft)

    @property
    def d(self):
        return json.loads(self.raw)

    async def screen(self):
        d = self.d
        out = await self.e._screen(d)
        self.raw = json.dumps(out.draft)
        return out

    async def cb(self, data):
        d = self.d
        out = await self.e.handle_callback(d, data)
        self.raw = json.dumps(out.draft)
        return out

    async def text(self, s):
        d = self.d
        out = await self.e.handle_text(d, s)
        self.raw = json.dumps(out.draft)
        return out

    async def pick(self, value):
        await self.screen()  # make sure options are current
        return await self.cb(f"qn:o:{opt_index(self.d, value)}")

    @property
    def step(self):
        return self.d["step"]


async def test_quick_flow_produces_profile(facets):
    e = Engine(facets, t)
    dr = Driver(e, e.new_draft("quick", lang="ru"))
    assert dr.step == "ui_language"
    await dr.pick("ru")
    assert dr.step == "target_roles"
    out = await dr.screen()
    assert "Information technology" in str([b.text for r in out.screen.rows for b in r])
    await dr.cb("qn:d")  # required → stays
    assert dr.step == "target_roles"
    await dr.cb("qn:g:1")
    await dr.pick("devops-sre")
    await dr.text("platform")  # nothing in the taxonomy → offered as own wording
    await dr.pick("free:platform")
    await dr.cb("qn:d")
    assert dr.step == "experience"
    await dr.pick("6-10")
    assert dr.step == "skills"
    await dr.text("kub")
    await dr.pick("Kubernetes")
    await dr.text("Terra")
    await dr.pick("Terraform")
    await dr.text("Crossplane")  # unknown to the crawler → free-text skill (never sent as API filter)
    await dr.pick("free:Crossplane")
    await dr.cb("qn:d")
    assert dr.step == "countries"
    await dr.pick("DE")
    await dr.pick("CH")
    await dr.cb("qn:d")
    assert dr.step == "work_models"
    await dr.pick("REMOTE")
    await dr.pick("HYBRID")
    await dr.cb("qn:d")
    assert dr.step == "remote_from"
    await dr.pick("DE")
    await dr.pick("FULL_TIME")
    await dr.cb("qn:d")
    assert dr.step == "languages"
    await dr.pick("de")
    await dr.pick("en")
    await dr.cb("qn:d")
    assert dr.step == "language_levels"
    out = await dr.screen()
    assert "Немецкий" in out.screen.text
    await dr.pick("B2")
    await dr.pick("C1")
    assert dr.step == "language_flexibility"
    await dr.pick("WARN")
    await dr.pick("70")
    await dr.pick("IMMEDIATE")
    assert dr.step == "confirm"
    out = await dr.screen()
    assert "Kubernetes" in out.screen.text and "DevOps / SRE" in out.screen.text
    done = await dr.cb("qn:ok")
    assert done.kind == "done"
    p = result_profile(dr.d)
    c = p.criteria
    assert [r.slug for r in c.roles] == ["devops-sre", None] and c.roles[1].free_text
    assert [(s.name, s.canonical) for s in c.skills] == [("Kubernetes", True), ("Terraform", True), ("Crossplane", False)]
    assert c.countries == ["DE", "CH"] and c.work_models == ["REMOTE", "HYBRID"] and c.remote_from == "DE"
    assert {(x.code, x.level) for x in c.languages} == {("de", "B2"), ("en", "C1")}
    assert c.search_breadth == "BALANCED" and c.min_score == 70  # quick-mode defaults
    assert p.notifications.mode == "IMMEDIATE" and p.notifications.quiet_start == "22:00"


async def test_back_any_skip_and_edit_from_summary(facets):
    e = Engine(facets, t)
    dr = Driver(e, e.new_draft("extended", lang="en"))
    await dr.pick("en")
    await dr.cb("qn:g:1")
    await dr.pick("devops-sre")
    await dr.cb("qn:d")
    assert dr.step == "current_title"
    await dr.cb("qn:b")
    assert dr.step == "target_roles"
    await dr.cb("qn:d")
    await dr.text("SRE")
    assert dr.d["a"]["current_title"] == "SRE"
    await dr.pick("<1")
    await dr.cb("qn:a")  # any seniority
    assert dr.d["a"]["preferred_seniority"] == []
    await dr.cb("qn:d")  # beginners may continue without skills
    assert dr.step == "additional_skills_text"
    # jump to the summary by editing an existing profile instead of answering 30 more questions
    answers = profile_to_answers(result_profile(dr.d))
    answers["work_models"] = ["ONSITE"]  # remote_from is not applicable for on-site only
    dr2 = Driver(e, e.new_draft("extended", lang="en", answers=answers))
    assert dr2.step == "confirm"
    out = await dr2.cb("qn:E")
    assert any(b.data == "qn:e:work_models" for r in out.screen.rows for b in r)
    await dr2.cb("qn:e:work_models")
    assert dr2.step == "work_models"
    await dr2.pick("ONSITE")  # untick
    await dr2.pick("REMOTE")
    await dr2.cb("qn:d")
    # remote_from is newly applicable and unanswered → asked, then straight back to the summary
    assert dr2.step == "remote_from"
    await dr2.pick("AT")
    assert dr2.step == "confirm"
    assert result_profile(dr2.d).criteria.remote_from == "AT"


async def test_extended_salary_and_per_item_steps(facets):
    e = Engine(facets, t)
    dr = Driver(e, e.new_draft("extended", lang="ru", answers=None))
    d = dr.d
    d["step"] = "salary_amount"
    dr.raw = json.dumps(d)
    out = await dr.text("abc")
    assert "Не понял число" in out.screen.text
    await dr.text("75k")
    assert dr.d["a"]["salary_amount"] == 75000
    assert dr.step == "salary_currency"
    await dr.pick("CHF")
    await dr.pick("year")
    await dr.pick("strict")
    await dr.pick("no")
    assert dr.step == "hard_constraints"
    s = result_profile(dr.d).criteria.salary
    assert s.amount == 75000 and s.currency == "CHF" and s.strict and not s.include_unknown


async def test_manual_flow_is_short(facets):
    e = Engine(facets, t)
    dr = Driver(e, e.new_draft("manual", lang="ru"))
    assert dr.step == "target_roles"
    seen = []
    await dr.cb("qn:g:1")
    await dr.pick("devops-sre")
    await dr.cb("qn:d")
    seen.append(dr.step)
    await dr.cb("qn:d")  # skills required? manual flow: experience unknown → required
    if dr.step == "skills":
        await dr.text("kub")
        await dr.pick("Kubernetes")
        await dr.cb("qn:d")
    seen.append(dr.step)
    await dr.pick("DE")
    await dr.cb("qn:d")
    await dr.cb("qn:a")
    assert dr.step == "confirm"
    assert seen == ["skills", "countries"]


async def test_facets_unavailable_falls_back_to_text(facets, fake):
    fake.down = True
    e = Engine(facets, t)
    dr = Driver(e, e.new_draft("quick", lang="ru"))
    await dr.pick("ru")
    out = await dr.screen()
    assert "недоступен" in out.screen.text
    await dr.text("DevOps")
    await dr.pick("free:DevOps")
    await dr.cb("qn:d")
    assert dr.step == "experience"
