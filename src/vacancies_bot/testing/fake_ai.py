"""Scripted OpenAI-compatible stand-in for tests and the offline demo.

It deliberately *misbehaves* in the ways real models do — invented skills, a level that was never written,
years without a number, explanation points without citations or with made-up numbers — so the guards are
exercised. Embeddings are deterministic bag-of-words vectors.
"""

from __future__ import annotations

import hashlib
import math
import re

from ..ai.provider import ChatResult

VOCAB_DIM = 64


class FakeAIProvider:
    name = "fake"

    def __init__(self, chat_model: str = "fake-chat", embed_model: str = "fake-embed") -> None:
        self.chat_model = chat_model
        self.embed_model = embed_model
        self.chat_calls = 0
        self.embed_calls = 0
        self.embedded_texts = 0
        self.fail = False

    async def chat_json(self, system: str, user: str, *, max_tokens: int = 800) -> ChatResult:
        self.chat_calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        if "extract a job seeker" in system:
            return ChatResult(self._extract(user), len(user) // 4, 200)
        if "explain to a job seeker" in system:
            return ChatResult(self._explain(user), len(user) // 4, 120)
        return ChatResult({}, 10, 2)

    def _extract(self, text: str) -> dict:
        low = text.lower()
        out: dict = {"roles": [], "skills": [], "languages": [], "experience_years": None, "work_models": [], "countries": [],
                     "seniority": None, "exclusions": [], "question": None}
        for skill in ("Kubernetes", "Terraform", "Helm", "Pulumi", "ArgoCD"):
            if skill.lower() in low:
                i = low.index(skill.lower())
                out["skills"].append({"name": skill, "evidence": text[i: i + len(skill)]})
        out["skills"].append({"name": "Rust", "evidence": "Rust expert"})  # invented: not in the text → must be dropped
        if "sre" in low:
            i = low.index("sre")
            out["roles"].append({"name": "Site Reliability Engineer", "evidence": text[i: i + 3]})
        m = re.search(r"(\d+)\s*(years|лет|jahre)", text, re.I)
        out["experience_years"] = {"years": int(m.group(1)), "evidence": m.group(0)} if m else {"years": 7, "evidence": "experienced"}
        if "german" in low or "немецк" in low or "deutsch" in low:
            i = next(low.index(w) for w in ("german", "немецк", "deutsch") if w in low)
            out["languages"].append({"code": "de", "level": "C1", "evidence": text[i: i + 7]})  # level not written → dropped
        if "english" in low or "английск" in low:
            m = re.search(r"(english|английский)\s*([ABC][12])", text, re.I)
            if m:
                out["languages"].append({"code": "en", "level": m.group(2), "evidence": m.group(0)})
        out["question"] = "Сколько лет вы работаете с Kubernetes?" if "kubernetes" in low else None
        return out

    def _explain(self, facts: str) -> dict:
        lines = {int(m.group(1)): m.group(2) for m in re.finditer(r"\[(\d+)\] (.*)", facts)}
        skills = next((i for i, f in lines.items() if f.startswith("Vacancy skills")), 1)
        cand = next((i for i, f in lines.items() if f.startswith("Candidate skills")), 1)
        return {
            "summary": "Хорошее совпадение по стеку.",
            "points": [
                {"text": "Совпадают ключевые технологии вакансии и ваши навыки.", "facts": [skills, cand]},
                {"text": "Зарплата 150000 EUR — выше рынка.", "facts": [skills]},  # invented number → dropped
                {"text": "Компания точно поддерживает визу.", "facts": []},  # no citation → dropped
                {"text": "Точка опоры", "facts": [999]},  # invalid id → dropped
            ],
        }

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        self.embed_calls += 1
        self.embedded_texts += len(texts)
        return [_bow(t) for t in texts], sum(len(t) // 4 for t in texts)


def _bow(text: str) -> list[float]:
    v = [0.0] * VOCAB_DIM
    for w in re.findall(r"[a-zа-яäöüß0-9+#]+", text.lower()):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16) % VOCAB_DIM
        v[h] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]
