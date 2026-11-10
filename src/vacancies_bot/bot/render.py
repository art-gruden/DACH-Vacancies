"""HTML rendering of profiles, vacancy cards and explanations (Telegram parse_mode=HTML)."""

from __future__ import annotations

from typing import Any

from ..crawler.models import JobOut
from ..domain.criteria import CandidateProfile, Provenance
from .texts import esc, t

MAX_CARD = 3500


def _join(items: list[str], lang: str) -> str:
    return ", ".join(items) if items else t(lang, "summary.any")


def profile_summary(p: CandidateProfile, lang: str, *, mode: str = "quick") -> str:
    c = p.criteria
    L = lambda key: t(lang, f"label.{key}")  # noqa: E731
    opt = lambda step, v: t(lang, f"opt.{step}.{v}", default=str(v))  # noqa: E731
    unconf = t(lang, "summary.unconfirmed")
    lines = [t(lang, "summary.title_manual" if mode == "manual" else "summary.title"), ""]
    roles = [esc(r.name) + (unconf if r.source != Provenance.USER_CONFIRMED else "") for r in c.roles]
    lines.append(f"🎯 {L('target_roles')}: {', '.join(roles) or '—'}")
    if c.current_title:
        lines.append(f"💼 {L('current_title')}: {esc(c.current_title)}")
    if c.experience:
        lines.append(f"⏳ {L('experience')}: {opt('experience', c.experience)}")
    if c.preferred_seniority:
        lines.append(f"📶 {L('preferred_seniority')}: {', '.join(opt('preferred_seniority', s) for s in c.preferred_seniority)}")
    if c.skills:
        sk = []
        for s in c.skills[:20]:
            item = esc(s.name)
            if s.core:
                item = f"<b>{item}</b>"
            if s.proficiency:
                item += f" ({opt('proficiency', s.proficiency)})"
            if s.source != Provenance.USER_CONFIRMED:
                item += unconf
            sk.append(item)
        lines.append(f"🛠 {L('skills')}: {', '.join(sk)}")
    where = [opt("country", x) for x in c.countries]
    if c.regions:
        where.append(", ".join(c.regions))
    if c.cities:
        where.append(", ".join(esc(x.name) for x in c.cities))
    lines.append(f"📍 {L('countries')}: {'; '.join(where)}")
    lines.append(f"🏠 {L('work_models')}: {_join([opt('work_models', w) for w in c.work_models], lang)}"
                 + (f" · {L('remote_from')}: {opt('remote_from', c.remote_from)}" if c.remote_from and c.wants_remote() else ""))
    lines.append(f"📄 {L('employment_types')}: {_join([opt('employment_types', e) for e in c.employment_types], lang)}")
    if c.languages:
        langs = ", ".join(f"{opt('lang', x.code)} {x.level or '?'}" for x in c.languages)
        lines.append(f"🌐 {L('languages')}: {langs} · {L('language_flexibility')}: {opt('language_flexibility', c.language_flexibility)}")
    if c.salary:
        kind = t(lang, "summary.strict" if c.salary.strict else "summary.soft")
        amount = f"{c.salary.amount:,.0f}".replace(",", " ")
        lines.append(f"💰 {L('salary_amount')}: ≥ {amount} {c.salary.currency} {opt('salary_period', c.salary.period)} ({kind})")
    if c.industries:
        lines.append(f"🏭 {L('industries')}: {', '.join(c.industries)}")
    if c.hard_constraints:
        lines.append(f"❗ {L('hard_constraints')}: {esc('; '.join(c.hard_constraints))}")
    if c.soft_preferences:
        lines.append(f"✨ {L('soft_preferences')}: {esc(', '.join(c.soft_preferences))}")
    if c.exclusions:
        lines.append(f"🚫 {L('exclusions')}: {esc(', '.join(c.exclusions))}")
    if mode != "manual":
        lines.append(f"🔭 {L('search_breadth')}: {opt('search_breadth', c.search_breadth)} · {L('min_score')}: {opt('min_score', str(c.min_score))}")
        n = p.notifications
        quiet = f"{n.quiet_start}–{n.quiet_end}" if n.quiet_start else opt("quiet_hours", "none")
        lines.append(f"🔔 {L('notification_mode')}: {opt('notification_mode', n.mode)} · {esc(n.timezone)} · {quiet} · ≤{n.daily_limit}/день" if lang == "ru"
                     else f"🔔 {L('notification_mode')}: {opt('notification_mode', n.mode)} · {esc(n.timezone)} · {quiet} · ≤{n.daily_limit}/day")
    extra = []
    if c.languages and c.language_flexibility == "NO":
        extra.append(L("languages").lower())
    if c.salary and c.salary.strict:
        extra.append(L("salary_amount").lower())
    if c.search_breadth == "STRICT":
        extra.append(L("target_roles").lower())
    lines += ["", f"<i>{t(lang, 'summary.footer', extra=(', ' + ', '.join(extra)) if extra else '')}</i>"]
    return "\n".join(lines)


def reason_text(r: dict[str, Any], lang: str) -> str:
    params = {}
    for k, v in (r.get("params") or {}).items():
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        elif isinstance(v, float):
            v = f"{v:,.0f}".replace(",", " ")
        params[k] = esc(v)
    return t(lang, f"r.{r['code']}", **params)


def score_label(score: int, lang: str) -> str:
    key = "card.score_high" if score >= 80 else "card.score_mid" if score >= 60 else "card.score_low"
    return t(lang, key)


def _salary(job: JobOut, lang: str) -> str:
    s = job.salary
    if not s or (s.min is None and s.max is None):
        return t(lang, "card.salary_ns")
    rng = "–".join(f"{x:,.0f}".replace(",", " ") for x in (s.min, s.max) if x is not None)
    value = f"{rng} {s.currency or '?'}" + (f" / {s.period}" if s.period else "")
    return t(lang, "card.salary", value=esc(value))


def job_card(job: JobOut, match: dict[str, Any], lang: str, *, header: str | None = None, sub_names: list[str] | None = None) -> str:
    score = match.get("score", 0)
    loc = "; ".join(", ".join(x for x in (loc.city, loc.region_name, loc.country) if x) for loc in job.locations[:2]) or "—"
    emp = ", ".join(e.replace("_", "-").title() for e in job.employment_types if e != "UNKNOWN") or "—"
    wm = job.work_model.title() if job.work_model != "UNKNOWN" else "—"
    lines = []
    if header:
        lines.append(header)
    lines.append(f"🧠 <b>{score}/100 — {score_label(score, lang)}</b>")
    lines.append(f"<b>{esc(job.title)}</b> — {esc(job.company.name)}")
    lines.append(f"📍 {esc(loc)} · {wm} · {esc(emp)}")
    langs = [f"{x.language.upper()} {x.level or x.descriptor or ''}".strip() + ("" if x.requirement == "required" else f" ({x.requirement})")
             for x in job.languages]
    lines.append("🌐 " + (", ".join(langs) if langs else t(lang, "card.lang_any")))
    lines.append("💰 " + _salary(job, lang))
    why = [reason_text(r, lang) for r in match.get("why", [])[:4]]
    if why:
        lines.append(f"✅ <b>{t(lang, 'card.why')}:</b> " + "; ".join(why))
    gaps = [reason_text(r, lang) for r in match.get("gaps", [])[:2]]
    if gaps:
        lines.append(f"⚠️ <b>{t(lang, 'card.gaps')}:</b> " + "; ".join(gaps))
    checks = [reason_text(r, lang) for r in (match.get("checks", []) + match.get("unknowns", []))[:3]]
    if checks:
        lines.append(f"🔍 <b>{t(lang, 'card.check')}:</b> " + "; ".join(checks))
    dates = [t(lang, "card.first_seen", date=job.first_seen_at.date().isoformat())]
    if job.published_at:
        dates.append(t(lang, "card.published", date=job.published_at.isoformat()))
    lines.append("🗓 " + " · ".join(dates))
    if sub_names:
        lines.append("🔔 " + t(lang, "card.subs", names=esc(", ".join(sub_names))))
    if job.status != "open":
        lines.append(t(lang, "card.closed"))
    return "\n".join(lines)[:MAX_CARD]


def explanation(job: JobOut, match: dict[str, Any], lang: str, llm: dict | None = None) -> str:
    comp = {k: int(round(100 * v)) for k, v in (match.get("components") or {}).items()}
    lines = [t(lang, "explain.title", score=match.get("score", 0)), f"<b>{esc(job.title)}</b> — {esc(job.company.name)}", ""]
    if comp:
        lines.append("<code>" + t(lang, "explain.components", **{k: f"{v}%" for k, v in comp.items()}) + "</code>")
    for key, title in (("why", "card.why"), ("gaps", "card.gaps"), ("unknowns", "card.unknown"), ("checks", "card.check")):
        items = match.get(key) or []
        if items:
            lines.append(f"\n<b>{t(lang, title)}</b>")
            lines += [f"• {reason_text(r, lang)}" for r in items[:8]]
    if match.get("failed"):
        lines.append("\n⛔ " + "; ".join(reason_text(r, lang) for r in match["failed"]))
    lines.append("\n" + t(lang, "card.completeness", p=int(100 * (match.get("completeness") or 0))))
    if llm and llm.get("points"):
        lines.append("\n" + t(lang, "card.ai_note"))
        if llm.get("summary"):
            lines.append(esc(llm["summary"]))
        lines += [f"• {esc(p['text'])}" for p in llm["points"][:6]]
    lines.append(f"\n<i>{t(lang, 'card.disclaimer')}</i>")
    return "\n".join(lines)[:MAX_CARD]
