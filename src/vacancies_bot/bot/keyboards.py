"""Inline keyboards as plain data (converted to aiogram markup in :mod:`bot.messenger`)."""

from __future__ import annotations

from ..questionnaire.engine import Button
from .texts import t


def job_buttons(lang: str, job_id: int, url: str, score: int, *, ctx: str, saved: bool = False) -> list[list[Button]]:
    """``ctx``: ``s<sub_id>`` (subscription match) or ``q<session_id>`` (search session)."""
    rows = [[Button(t(lang, "card.open"), url=url)]]
    row = [Button(t(lang, "card.why_btn", score=score), f"j:w:{job_id}:{ctx}")]
    if not saved:
        row.append(Button(t(lang, "card.save"), f"j:s:{job_id}:{ctx}"))
    rows.append(row)
    rows.append([Button(t(lang, "card.like"), f"j:l:{job_id}:{ctx}"), Button(t(lang, "card.dislike"), f"j:n:{job_id}:{ctx}"),
                 Button(t(lang, "card.applied"), f"j:a:{job_id}:{ctx}")])
    if ctx.startswith("s"):
        rows.append([Button(t(lang, "card.adjust"), f"sub:open:{ctx[1:]}")])
    return rows


def dislike_reasons(lang: str, job_id: int, ctx: str) -> list[list[Button]]:
    reasons = ["role", "language", "salary", "geo", "level", "other"]
    btns = [Button(t(lang, f"fb.reason.{r}"), f"j:r:{job_id}:{ctx}:{r}") for r in reasons]
    return [btns[i: i + 2] for i in range(0, len(btns), 2)]


def main_menu(lang: str) -> list[list[Button]]:
    return [
        [Button(t(lang, "menu.search"), "m:search"), Button(t(lang, "menu.subs"), "m:subs")],
        [Button(t(lang, "menu.profile"), "m:profile"), Button(t(lang, "menu.saved"), "m:saved")],
        [Button(t(lang, "menu.ask"), "m:ask"), Button(t(lang, "menu.privacy"), "m:privacy")],
    ]


def start_menu(lang: str) -> list[list[Button]]:
    return [
        [Button(t(lang, "start.quick"), "st:quick")],
        [Button(t(lang, "start.extended"), "st:extended")],
        [Button(t(lang, "start.manual"), "st:manual")],
    ]
