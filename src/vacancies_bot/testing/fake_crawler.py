"""In-memory stand-in for the crawler's Bot API, served through ``httpx.MockTransport``.

It follows the documented semantics of ``/api/bot/v1`` (AND across filters, OR within one, unknown values only
with ``include_unknown``, language fit, keyset-style opaque cursors bound to the query, the change feed with
400/409) closely enough to test the bot end to end without network access. It is *not* the crawler.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ..crawler.models import JobDetail
from ..domain.matching import language_fit

TOKEN = "test-token-0123456789abcdef0123456789abcdef"

GROUPS = [
    {"id": 1, "slug": "information-technology", "name": "Information technology", "name_de": "Informationstechnologie", "is_it": True},
    {"id": 2, "slug": "healthcare", "name": "Healthcare", "name_de": "Gesundheitswesen", "is_it": False},
    {"id": 3, "slug": "finance", "name": "Finance & accounting", "name_de": "Finanzen", "is_it": False},
    {"id": 4, "slug": "engineering", "name": "Engineering & industry", "name_de": "Ingenieurwesen", "is_it": False},
]
LEAVES = [
    {"id": 10, "slug": "devops-sre", "name": "DevOps / SRE", "name_de": "DevOps / SRE", "group": "information-technology", "is_it": True},
    {"id": 11, "slug": "backend-development", "name": "Backend development", "name_de": "Backend-Entwicklung", "group": "information-technology", "is_it": True},
    {"id": 12, "slug": "data-engineering", "name": "Data engineering", "name_de": "Data Engineering", "group": "information-technology", "is_it": True},
    {"id": 13, "slug": "frontend-development", "name": "Frontend development", "name_de": "Frontend-Entwicklung", "group": "information-technology", "is_it": True},
    {"id": 20, "slug": "nursing", "name": "Nursing", "name_de": "Pflege", "group": "healthcare", "is_it": False},
    {"id": 30, "slug": "accounting", "name": "Accounting", "name_de": "Buchhaltung", "group": "finance", "is_it": False},
    {"id": 40, "slug": "mechanical-engineering", "name": "Mechanical engineering", "name_de": "Maschinenbau", "group": "engineering", "is_it": False},
]
REGIONS = {
    "DE": [("DE-BE", "Berlin"), ("DE-BY", "Bayern"), ("DE-HH", "Hamburg"), ("DE-HE", "Hessen"), ("DE-SN", "Sachsen")],
    "AT": [("AT-9", "Wien"), ("AT-6", "Steiermark")],
    "CH": [("CH-ZH", "Zürich"), ("CH-BS", "Basel-Stadt")],
}
CITIES = {
    1: ("Berlin", "DE", "DE-BE"), 2: ("München", "DE", "DE-BY"), 3: ("Hamburg", "DE", "DE-HH"), 4: ("Frankfurt am Main", "DE", "DE-HE"),
    5: ("Dresden", "DE", "DE-SN"), 6: ("Leipzig", "DE", "DE-SN"), 7: ("Wien", "AT", "AT-9"), 8: ("Graz", "AT", "AT-6"),
    9: ("Zürich", "CH", "CH-ZH"), 10: ("Basel", "CH", "CH-BS"),
}
SKILL_CATEGORIES = {
    "Kubernetes": "platform", "Terraform": "tool", "Ansible": "tool", "GitLab CI": "tool", "Go": "language", "Python": "language",
    "Java": "language", "Spring": "framework", "AWS": "cloud", "Spark": "data", "Prometheus": "tool", "Linux": "platform",
    "Docker": "platform", "Helm": "tool", "Pulumi": "tool", "ArgoCD": "tool", "Grafana": "tool",
}
NOW = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)


def _spec(slug: str | None, conf: float = 0.8) -> dict | None:
    if slug is None:
        return None
    leaf = next(x for x in LEAVES if x["slug"] == slug)
    return {k: leaf[k] for k in ("id", "slug", "name", "name_de", "group", "is_it")} | {"confidence": conf}


def make_job(
    job_id: int,
    title: str,
    *,
    company: str = "Example GmbH",
    city: int | None = None,
    work_model: str = "ONSITE",
    remote_scope: str = "unknown",
    remote_countries: list[str] | None = None,
    employment: list[str] | None = None,
    spec: str | None = "devops-sre",
    secondary: list[str] | None = None,
    seniority: str = "UNKNOWN",
    skills: list[str] | None = None,
    languages: list[dict] | None = None,
    salary: dict | None = None,
    status: str = "open",
    age_days: float = 1.0,
    description: str | None = None,
    completeness: float = 0.8,
) -> dict:
    locs = []
    if city is not None:
        name, country, region = CITIES[city]
        rname = dict(REGIONS[country])[region]
        locs.append(
            {"id": city, "country": country, "country_name": {"DE": "Germany", "AT": "Austria", "CH": "Switzerland"}[country],
             "region": region, "region_name": rname, "city": name, "postcode": None, "is_primary": True, "confidence": 0.9,
             "source": "gazetteer"}
        )
    seen = NOW - timedelta(days=age_days)
    missing = []
    if salary is None:
        missing.append("salary")
    if seniority == "UNKNOWN":
        missing.append("seniority")
    return {
        "id": job_id,
        "title": title,
        "company": {"id": 100 + job_id, "name": company, "domain": f"{company.split()[0].lower()}.example", "country": locs[0]["country"] if locs else None},
        "url": f"https://jobs.example/{job_id}",
        "application_url": None,
        "status": status,
        "published_at": None,
        "first_seen_at": seen.isoformat(),
        "last_seen_at": NOW.isoformat(),
        "last_checked_at": NOW.isoformat(),
        "updated_at": seen.isoformat(),
        "closed_at": NOW.isoformat() if status != "open" else None,
        "geo_status": "resolved" if locs else "unknown",
        "locations": locs,
        "work_model": work_model,
        "remote_eligibility": {"scope": remote_scope, "countries": remote_countries or []},
        "employment_types": employment or ["UNKNOWN"],
        "specialization": _spec(spec),
        "secondary_specializations": [_spec(s, 0.5) for s in (secondary or [])],
        "seniority": seniority,
        "skills": [{"name": s, "category": SKILL_CATEGORIES.get(s)} for s in (skills or [])],
        "languages": languages or [],
        "salary": salary,
        "quality": {"score": 0.8, "completeness": completeness, "missing": missing, "field_confidence": {}},
        "language_fit": None,
        "relevance": None,
        "description": description or f"{title}. We are looking for a colleague. " + ", ".join(skills or []),
        "requirements": [],
        "responsibilities": [],
    }


def L(lang: str, level: str | None, req: str = "required") -> dict:  # noqa: N802 - short helper for fixtures
    return {"language": lang, "level": level, "requirement": req, "descriptor": None, "evidence": None, "confidence": 0.9}


def S(lo: float | None, hi: float | None, cur: str | None, period: str = "year") -> dict:  # noqa: N802
    return {"min": lo, "max": hi, "currency": cur, "period": period, "basis": "gross", "raw": f"{lo}-{hi} {cur}", "confidence": 0.9}


def default_jobs() -> list[dict]:
    return [
        make_job(1, "Site Reliability Engineer (m/w/d)", company="Alpen Cloud AG", city=9, work_model="HYBRID", employment=["FULL_TIME"],
                 seniority="SENIOR", skills=["Kubernetes", "Terraform", "Go", "Prometheus"], languages=[L("de", "B2"), L("en", "C1")],
                 salary=S(120000, 140000, "CHF"), age_days=2),
        make_job(2, "DevOps Engineer", company="Spree Software GmbH", city=1, work_model="REMOTE", remote_scope="countries",
                 remote_countries=["DE"], employment=["FULL_TIME"], seniority="MID", skills=["Kubernetes", "Ansible", "GitLab CI"],
                 languages=[L("en", "B2")], age_days=1),
        make_job(3, "Platform Engineer Kubernetes", company="Elbe Energie GmbH", city=5, work_model="ONSITE", spec="backend-development",
                 secondary=["devops-sre"], employment=["FULL_TIME"], seniority="SENIOR", skills=["Kubernetes", "Python"],
                 languages=[L("de", "C1")], salary=S(65000, 75000, "EUR"), age_days=3),
        make_job(4, "Werkstudent DevOps (m/w/d)", company="Isar Tech GmbH", city=2, work_model="HYBRID", employment=["WORKING_STUDENT"],
                 seniority="INTERN", skills=["Docker", "Linux"], age_days=1),
        make_job(5, "Backend Developer Java", company="Donau Digital GmbH", city=7, work_model="HYBRID", spec="backend-development",
                 employment=["FULL_TIME"], seniority="MID", skills=["Java", "Spring"], languages=[L("de", "B2")], age_days=4),
        make_job(6, "Cloud Engineer (Remote)", company="Nomad Ops GmbH", city=None, work_model="REMOTE", remote_scope="unknown",
                 seniority="UNKNOWN", skills=["Terraform", "AWS"], age_days=2, completeness=0.5),
        make_job(7, "Pflegefachkraft (m/w/d)", company="Klinikum Dresden", city=5, spec="nursing", employment=["FULL_TIME", "PART_TIME"],
                 seniority="UNKNOWN", languages=[L("de", "B2")], age_days=1),
        make_job(8, "Lead SRE", company="Hanse Logistik GmbH", city=3, work_model="ONSITE", employment=["FULL_TIME"], seniority="LEAD",
                 skills=["Kubernetes", "Terraform", "Helm"], languages=[L("de", "C2")], salary=S(95000, None, "EUR"), age_days=5),
        make_job(9, "Data Engineer", company="Messe Data GmbH", city=6, work_model="HYBRID", spec="data-engineering", employment=["FULL_TIME"],
                 seniority="MID", skills=["Python", "Spark", "Kubernetes"], age_days=2),
        make_job(10, "DevOps Engineer (Contract)", company="Murtal IT GmbH", city=8, work_model="REMOTE", remote_scope="dach",
                 remote_countries=["DE", "AT", "CH"], employment=["CONTRACT"], seniority="SENIOR", skills=["Kubernetes", "Terraform", "Ansible"], languages=[L("en", None)], age_days=1),
        make_job(11, "Buchhalter (m/w/d)", company="Limmat Treuhand AG", city=9, spec="accounting", employment=["FULL_TIME"], seniority="MID",
                 languages=[L("de", "C1")], salary=S(85000, 95000, "CHF"), age_days=6),
        make_job(12, "Site Reliability Engineer", company="Rhein Pharma AG", city=10, work_model="ONSITE", employment=["FULL_TIME"],
                 seniority="SENIOR", skills=["Kubernetes", "Prometheus"], age_days=7),
        make_job(13, "DevOps Engineer (closed)", company="Old Corp GmbH", city=1, status="closed", employment=["FULL_TIME"], seniority="MID",
                 skills=["Kubernetes"], age_days=30),
        make_job(14, "Kubernetes Administrator", company="Main Hosting GmbH", city=4, work_model="HYBRID", spec=None, employment=["FULL_TIME"],
                 seniority="UNKNOWN", skills=["Kubernetes", "Linux"], age_days=2),
    ]


def _enc(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).decode().rstrip("=")


def _dec(s: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))


def _err(code: int, detail: Any, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(code, json={"detail": detail}, headers=headers or {})


class FakeCrawler:
    def __init__(self, jobs: list[dict] | None = None, token: str = TOKEN) -> None:
        self.jobs: dict[int, dict] = {j["id"]: j for j in (jobs if jobs is not None else default_jobs())}
        self.events: list[dict] = []
        self.token = token
        self.disabled = False
        self.down = False
        self.rate_limit_next = 0
        self.requests: Counter[str] = Counter()
        self.transport = httpx.MockTransport(self.handle)

    # --- test helpers ---------------------------------------------------------------------------------
    def add_job(self, job: dict, *, event: bool = True) -> None:
        self.jobs[job["id"]] = job
        if event:
            self.emit("job.created", job["id"])

    def emit(self, event_type: str, job_id: int, *, fields: list[str] | None = None, reason: str | None = None) -> int:
        eid = (self.events[-1]["event_id"] if self.events else 0) + 1
        self.events.append({"event_id": eid, "event_type": event_type, "job_id": job_id, "occurred_at": NOW.isoformat(),
                            "fields": fields, "reason": reason})
        return eid

    def update_job(self, job_id: int, **changes) -> None:
        self.jobs[job_id].update(changes)
        self.emit("job.updated", job_id, fields=sorted(changes))

    def close_job(self, job_id: int, reason: str = "missed") -> None:
        self.jobs[job_id]["status"] = "closed"
        self.emit("job.closed", job_id, reason=reason)

    # --- transport --------------------------------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        prefix = "/api/bot/v1"
        path = path[len(prefix):] if path.startswith(prefix) else path
        self.requests[path.split("/")[1] if path.count("/") >= 1 else path] += 1
        if self.down:
            return _err(502, "bad gateway")
        if self.disabled:
            return _err(503, "Bot API is disabled")
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {self.token}":
            return _err(401, "Invalid or missing API token", {"WWW-Authenticate": "Bearer"})
        if self.rate_limit_next > 0:
            self.rate_limit_next -= 1
            return _err(429, "Rate limit exceeded", {"Retry-After": "1"})
        qp = request.url.params
        try:
            if path == "/jobs":
                return self._jobs(qp)
            if path.startswith("/jobs/"):
                job = self.jobs.get(int(path.rsplit("/", 1)[1]))
                return httpx.Response(200, json=job) if job else _err(404, "Vacancy not found")
            if path == "/facets/countries":
                return httpx.Response(200, json=self._countries())
            if path == "/facets/regions":
                return httpx.Response(200, json=self._regions(qp["country"].upper()))
            if path == "/facets/cities":
                return httpx.Response(200, json=self._cities(qp))
            if path == "/facets/specializations":
                return httpx.Response(200, json=self._specs())
            if path == "/facets/skills":
                return httpx.Response(200, json=self._skills(qp))
            if path == "/changes/head":
                return httpx.Response(200, json={"cursor": _enc({"v": 1, "e": self._head()})})
            if path == "/changes":
                return self._changes(qp)
        except ValueError as exc:
            return _err(422, str(exc))
        return _err(404, "Not Found")

    # --- facets ---------------------------------------------------------------------------------------------
    def _open(self) -> list[dict]:
        return [j for j in self.jobs.values() if j["status"] == "open"]

    def _countries(self) -> list[dict]:
        c = Counter(loc["country"] for j in self._open() for loc in {x["country"]: x for x in j["locations"]}.values())
        names = {"DE": ("Germany", "Deutschland"), "AT": ("Austria", "Österreich"), "CH": ("Switzerland", "Schweiz")}
        return [{"code": k, "name": v[0], "name_local": v[1], "in_dach": True, "open_jobs": c.get(k, 0), "companies": 0} for k, v in names.items()]

    def _regions(self, country: str) -> list[dict]:
        if country not in REGIONS:
            raise ValueError(f"unknown country '{country}'")
        c = Counter(loc["region"] for j in self._open() for loc in j["locations"])
        return [{"code": code, "country": country, "name": n, "name_local": n, "kind": "state", "open_jobs": c.get(code, 0)}
                for code, n in REGIONS[country]]

    def _cities(self, qp) -> list[dict]:
        country = qp["country"].upper()
        region = qp.get("region")
        prefix = (qp.get("q") or "").casefold().replace("ue", "ü")
        c = Counter(loc["id"] for j in self._open() for loc in j["locations"])
        out = []
        for cid, (name, cc, reg) in CITIES.items():
            if cc != country or (region and reg != region) or not c.get(cid):
                continue
            if prefix and not name.casefold().startswith(prefix):
                continue
            out.append({"id": cid, "city": name, "country": cc, "region": reg, "open_jobs": c[cid]})
        return sorted(out, key=lambda x: (-x["open_jobs"], x["city"]))[: int(qp.get("limit", 100))]

    def _specs(self) -> list[dict]:
        leaf_c: Counter = Counter()
        group_c: Counter = Counter()
        for j in self._open():
            specs = [s for s in [j["specialization"], *j["secondary_specializations"]] if s]
            for s in specs:
                leaf_c[s["id"]] += 1
            for g in {s["group"] for s in specs}:
                group_c[g] += 1
        out = []
        for g in GROUPS:
            kids = [{**{k: x[k] for k in ("id", "slug", "name", "name_de", "is_it")}, "open_jobs": leaf_c.get(x["id"], 0), "children": []}
                    for x in LEAVES if x["group"] == g["slug"]]
            out.append({**g, "open_jobs": group_c.get(g["slug"], 0), "children": kids})
        return out

    def _skills(self, qp) -> list[dict]:
        c = Counter(s["name"] for j in self._open() for s in j["skills"])
        for name in SKILL_CATEGORIES:
            c.setdefault(name, 0)
        q = (qp.get("q") or "").casefold()
        items = [(n, k) for n, k in c.items() if not q or n.casefold().startswith(q)]
        items.sort(key=lambda x: (-x[1], x[0]))
        return [{"name": n, "category": SKILL_CATEGORIES.get(n), "open_jobs": k} for n, k in items[: int(qp.get("limit", 50))]]

    # --- search ----------------------------------------------------------------------------------------------
    def _jobs(self, qp) -> httpx.Response:
        many = lambda k: [v for x in qp.get_list(k) for v in x.split(",") if v]  # noqa: E731
        inc = set(many("include_unknown"))
        countries, regions = many("country"), many("region")
        cities = [int(x) for x in many("city")]
        specs = set(many("specialization"))
        groups_selected = {g["slug"] for g in GROUPS} & specs
        skills = many("skills")
        known_sk = {k.casefold(): k for k in SKILL_CATEGORIES}
        bad = [s for s in skills if s.casefold() not in known_sk]
        if bad:
            return _err(422, f"skills: unknown {', '.join(bad)}; see /facets/skills")
        langs: dict[str, str | None] = {}
        if qp.get("languages"):
            for item in qp["languages"].split(","):
                code, _, lvl = item.partition(":")
                langs[code.strip().lower()] = lvl.strip().upper() or None
        lang_mode = qp.get("lang_mode", "inclusive")
        salary_min = float(qp["salary_min"]) if qp.get("salary_min") else None
        currency = qp.get("currency")
        if salary_min is not None and not currency:
            return _err(422, "salary_min requires currency")
        period = qp.get("salary_period", "year")
        remote_in = many("remote_in")
        status = qp.get("status", "open")
        q = (qp.get("q") or "").casefold()
        new_since = datetime.fromisoformat(qp["new_since"]) if qp.get("new_since") else None
        exclude = [x.casefold() for x in many("exclude")]

        def ok(j: dict) -> bool:
            if status != "all" and j["status"] != status:
                return False
            if new_since and datetime.fromisoformat(j["first_seen_at"]) <= new_since:
                return False
            text = (j["title"] + " " + (j["description"] or "")).casefold()
            if q:
                alts = [a.strip().strip('"') for a in q.split(" or ")]
                if not any(a and a in text for a in alts):
                    return False
            if any(x in text for x in exclude):
                return False
            if countries or regions or cities:
                if not j["locations"]:
                    if "location" not in inc:
                        return False
                elif not any(
                    (not countries or loc["country"] in countries) and (not regions or loc["region"] in regions)
                    and (not cities or loc["id"] in cities) for loc in j["locations"]
                ):
                    return False
            wm = many("work_model")
            if wm and not (j["work_model"] in wm or (j["work_model"] == "UNKNOWN" and "work_model" in inc)):
                return False
            emp = many("employment")
            if emp and not (set(emp) & set(j["employment_types"]) or ("UNKNOWN" in j["employment_types"] and "employment" in inc)):
                return False
            sen = many("seniority")
            if sen and not (j["seniority"] in sen or (j["seniority"] == "UNKNOWN" and "seniority" in inc)):
                return False
            if specs:
                js = [s for s in [j["specialization"], *j["secondary_specializations"]] if s]
                hit = any(s["slug"] in specs or s["group"] in groups_selected for s in js)
                if not hit and not (j["specialization"] is None and "specialization" in inc):
                    return False
            if skills:
                names = {s["name"].casefold() for s in j["skills"]}
                want = {known_sk[s.casefold()].casefold() for s in skills}
                if qp.get("skills_mode", "any") == "all" and not want <= names:
                    return False
                if qp.get("skills_mode", "any") == "any" and not want & names:
                    return False
            if langs:
                fit = language_fit(langs, JobDetail.model_validate(j))
                if fit == "exceeds" or (lang_mode == "strict" and fit != "compatible"):
                    return False
            if currency:
                s = j["salary"]
                val = (s["max"] if s and s["max"] is not None else (s["min"] if s else None))
                known = s and s["currency"] == currency and s["period"] == period and val is not None
                if known and salary_min is not None:
                    known = val >= salary_min
                if not known and not (s is None and "salary" in inc):
                    return False
            if remote_in:
                remote_job = j["work_model"] in ("REMOTE", "HYBRID")
                el = j["remote_eligibility"]
                eligible = remote_job and (el["scope"] == "worldwide" or set(el["countries"]) & set(remote_in))
                if not eligible and not (remote_job and el["scope"] == "unknown" and "remote" in inc):
                    return False
            return True

        rows = [j for j in self.jobs.values() if ok(j)]
        rows.sort(key=lambda j: (j["first_seen_at"], j["id"]), reverse=True)
        lang_fit = {j["id"]: language_fit(langs, JobDetail.model_validate(j)) for j in rows} if langs else {}
        params_key = hashlib.sha1(json.dumps(sorted((k, v) for k, v in qp.multi_items() if k not in ("cursor", "limit"))).encode()).hexdigest()[:12]
        offset = 0
        if qp.get("cursor"):
            cur = _dec(qp["cursor"])
            if cur.get("h") != params_key:
                return _err(422, "cursor belongs to a different query")
            offset = cur["o"]
        limit = int(qp.get("limit", 25))
        page = rows[offset: offset + limit]
        more = offset + limit < len(rows)
        items = []
        for j in page:
            item = {k: v for k, v in copy.deepcopy(j).items() if k not in ("description", "requirements", "responsibilities")}
            item["language_fit"] = lang_fit.get(j["id"])
            items.append(item)
        return httpx.Response(200, json={"items": items, "next_cursor": _enc({"v": 1, "o": offset + limit, "h": params_key}) if more else None,
                                         "has_more": more})

    # --- feed ------------------------------------------------------------------------------------------------
    def _head(self) -> int:
        return self.events[-1]["event_id"] if self.events else 0

    def _changes(self, qp) -> httpx.Response:
        after = 0
        if qp.get("cursor"):
            try:
                after = int(_dec(qp["cursor"])["e"])
            except Exception:
                return _err(400, "invalid cursor")
        if after > self._head():
            return _err(409, "cursor is ahead of the event log (was the database restored?)")
        limit = int(qp.get("limit", 100))
        rows = [e for e in self.events if e["event_id"] > after][: limit + 1]
        more = len(rows) > limit
        rows = rows[:limit]
        return httpx.Response(200, json={"items": rows, "next_cursor": _enc({"v": 1, "e": rows[-1]["event_id"] if rows else after}),
                                         "has_more": more})
