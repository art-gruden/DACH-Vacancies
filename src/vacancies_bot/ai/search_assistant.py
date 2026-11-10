"""AI Search Assistant: a free-text request → criteria the user sees before searching.

It is a *manual* search: the active subscriptions are not changed unless the user presses "Save as
subscription". Works without AI (rule-based facts); with AI + consent the LLM facts are added, under the
same evidence checks as the profile parser.
"""

from __future__ import annotations

from ..domain.criteria import CandidateProfile, SearchCriteria
from ..questionnaire.convert import answers_to_profile, profile_to_answers
from .profile_parser import Fact, ParseResult, ProfileParser, apply_facts

RESET_KEYS = {
    "role": ["target_roles"], "skill": ["skills", "core_skills", "proficiency"], "language": ["languages", "language_levels"],
    "work_model": ["work_models"], "country": ["countries", "regions", "cities"], "remote_from": ["remote_from"],
    "seniority": ["preferred_seniority"], "experience": ["experience"],
}


class SearchAssistant:
    def __init__(self, parser: ProfileParser) -> None:
        self.parser = parser

    async def interpret(self, text: str, base: CandidateProfile | None, *, user_id: int | None, use_ai: bool) -> tuple[SearchCriteria, ParseResult]:
        result = await self.parser.parse(text, user_id=user_id, use_ai=use_ai, source="text")
        answers = profile_to_answers(base) if base else {"countries": ["DE", "AT", "CH"]}
        kinds = {f.kind for f in result.facts}
        for kind in kinds:
            for key in RESET_KEYS.get(kind, []):
                answers.pop(key, None)
        if "language" in kinds:
            answers["language_flexibility"] = answers.get("language_flexibility") or "WARN"
        answers, _ = apply_facts(answers, result.facts)
        if "remote_from" in kinds and "work_model" not in kinds:
            answers["work_models"] = ["REMOTE"]
        if "remote_from" in kinds and "country" not in kinds:
            # "remote from Germany" names where the candidate lives, not where the employer must be
            answers["countries"] = sorted({*(answers.get("countries") or []), next(f.value["code"] for f in result.facts if f.kind == "remote_from")})
        criteria = answers_to_profile(answers, "manual").criteria
        criteria.min_score = base.criteria.min_score if base else 0
        return criteria, result


def describe(facts: list[Fact]) -> list[str]:
    return [f"{f.kind}: {f.value}" for f in facts]
