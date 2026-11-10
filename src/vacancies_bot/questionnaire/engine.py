"""Questionnaire state machine, independent of Telegram (the bot layer only sends :class:`Screen` objects).

The whole state is a JSON-serialisable *draft* stored in ``questionnaire_drafts`` after every answer, so a
restart or a deploy never loses a half-filled questionnaire. Callback data is short (``qn:<action>:<arg>``)
and refers to the option list saved in the draft, which keeps it below Telegram's 64-byte limit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..services.facets import FacetsService
from .convert import answers_to_profile, parse_amount
from .steps import BY_KEY, Step, applicable, flow, is_required

PAGE = 12
FLAGS = {"DE": "🇩🇪", "AT": "🇦🇹", "CH": "🇨🇭"}


@dataclass
class Button:
    text: str
    data: str | None = None
    url: str | None = None


@dataclass
class Screen:
    text: str
    rows: list[list[Button]] = field(default_factory=list)
    expects_text: bool = False


@dataclass
class Outcome:
    kind: str  # screen | done | cancelled
    draft: dict
    screen: Screen | None = None
    notice: str | None = None


Translator = Callable[..., str]


class Engine:
    def __init__(self, facets: FacetsService, tr: Translator) -> None:
        self.facets = facets
        self.tr = tr

    # --- lifecycle -----------------------------------------------------------------------------------------

    def new_draft(self, mode: str, *, lang: str, purpose: str = "profile", answers: dict | None = None) -> dict:
        d = {"mode": mode, "purpose": purpose, "lang": lang, "a": dict(answers or {}), "hist": [], "opts": [], "page": 0,
             "group": None, "item": 0, "rtc": False, "step": None, "q": None, "seen": []}
        if answers and mode != "manual":
            d["a"].setdefault("ui_language", lang)
        first = self._next_key(d, None)
        d["step"] = first
        if d["a"] and mode != "manual" and "ui_language" in d["a"]:
            # editing an existing profile starts at the summary; every step of the flow counts as seen
            d["step"] = "confirm"
            d["seen"] = [s.key for s in flow(mode) if applicable(s, d["a"])]
        return d

    def _t(self, d: dict, key: str, **kw) -> str:
        return self.tr(d.get("lang") or "ru", key, **kw)

    def _opt(self, d: dict, step: str, value: str) -> str:
        return self.tr(d.get("lang") or "ru", f"opt.{step}.{value}", default=value)

    def _flow(self, d: dict) -> list[Step]:
        return flow(d["mode"])

    def _next_key(self, d: dict, current: str | None) -> str:
        steps = self._flow(d)
        keys = [s.key for s in steps]
        start = keys.index(current) + 1 if current in keys else 0
        for s in steps[start:]:
            if s.key == "confirm":
                return "confirm"
            if not applicable(s, d["a"]):
                continue
            if d.get("rtc") and (s.key in d["a"] or s.key in d.get("seen", [])):
                continue
            if s.key == "ui_language" and d["mode"] == "manual":
                continue
            return s.key
        return "confirm"

    def _go(self, d: dict, key: str, *, push: bool = True) -> None:
        if d["step"] and d["step"] not in d.setdefault("seen", []):
            d["seen"].append(d["step"])
        if push and d["step"]:
            d["hist"].append(d["step"])
        d.update(step=key, opts=[], page=0, group=None, item=0, q=None)

    def _advance(self, d: dict) -> None:
        self._go(d, self._next_key(d, d["step"]))

    # --- input -------------------------------------------------------------------------------------------------

    async def handle_callback(self, d: dict, data: str) -> Outcome:
        parts = data.split(":", 2)
        action = parts[1] if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""
        step = BY_KEY[d["step"]]
        notice = None

        if action == "x":
            return Outcome("cancelled", d)
        if action == "b":
            if d["hist"]:
                prev = d["hist"].pop()
                self._go(d, prev, push=False)
            return await self._screen(d)
        if action == "p":
            d["page"] = max(0, int(arg or 0))
            return await self._screen(d)
        if action == "G":
            d["group"] = None
            d["q"] = None
            d["page"] = 0
            return await self._screen(d)
        if action == "g":
            d["group"] = int(arg)
            d["page"] = 0
            return await self._screen(d)
        if action == "e":  # edit one step from the summary
            if arg in BY_KEY:
                d["rtc"] = True
                self._go(d, arg)
            return await self._screen(d)
        if action == "E":  # show the list of editable steps
            d["step"] = "confirm"
            d["q"] = "edit"
            return await self._screen(d)
        if action == "ok" and step.kind == "confirm":
            return Outcome("done", d)
        if action == "s":
            if is_required(step, d["a"]):
                notice = self._t(d, "q.required")
            else:
                if step.kind != "per_item" and step.key not in d["a"]:
                    d["a"][step.key] = [] if step.kind in ("multi", "skills", "spec", "regions", "cities") else None
                self._advance(d)
            return await self._screen(d, notice)
        if action == "a":  # "any" for multi steps
            d["a"][step.key] = []
            self._advance(d)
            return await self._screen(d)
        if action == "n":  # "none" (e.g. no languages to state)
            d["a"][step.key] = []
            self._advance(d)
            return await self._screen(d)
        if action == "d":  # done for multi-select steps
            sel = d["a"].get(step.key)
            if not sel and is_required(step, d["a"]):
                return await self._screen(d, self._t(d, "q.pick_one"))
            if step.key == "skills" and not sel:
                d["a"]["skills"] = []
            self._advance(d)
            return await self._screen(d)
        if action == "o":
            idx = int(arg)
            if idx >= len(d["opts"]):
                return await self._screen(d)
            value = d["opts"][idx][0]
            notice = await self._select(d, step, value)
            return await self._screen(d, notice)
        return await self._screen(d)

    async def handle_text(self, d: dict, text: str) -> Outcome:
        step = BY_KEY[d["step"]]
        text = text.strip()
        if not text:
            return await self._screen(d)
        if step.kind == "text":
            if step.key == "salary_amount":
                amount = parse_amount(text)
                if amount is None:
                    return await self._screen(d, self._t(d, "q.bad_number"))
                d["a"][step.key] = amount
            else:
                limit = step.meta.get("max", 500)
                d["a"][step.key] = text[:limit]
            self._advance(d)
            return await self._screen(d)
        if step.kind in ("spec", "skills", "cities"):
            d["q"] = text[:60]
            d["page"] = 0
            d["group"] = None
            return await self._screen(d)
        if step.key == "languages":
            code = text.strip().lower()[:3]
            if code.isalpha() and 2 <= len(code) <= 3:
                sel = d["a"].setdefault("languages", [])
                if code not in sel:
                    sel.append(code)
            return await self._screen(d)
        if step.key == "timezone":
            from zoneinfo import ZoneInfo

            try:
                ZoneInfo(text)
            except Exception:
                return await self._screen(d, self._t(d, "q.bad_timezone"))
            d["a"]["timezone"] = text
            self._advance(d)
            return await self._screen(d)
        return await self._screen(d, self._t(d, "q.use_buttons"))

    async def _select(self, d: dict, step: Step, value: str) -> str | None:
        a = d["a"]
        if step.kind == "single":
            a[step.key] = value
            if step.key == "ui_language":
                d["lang"] = value
            if step.key == "cv" and value == "upload":
                a["cv"] = "upload"
            self._advance(d)
            return None
        if step.kind == "per_item":
            items = self._items(d, step)
            if d["item"] < len(items):
                a.setdefault(step.key, {})[items[d["item"]]] = value
                d["item"] += 1
            if d["item"] >= len(items):
                self._advance(d)
            return None
        if step.kind == "multi":
            sel = list(a.get(step.key) or [])
            if value in sel:
                sel.remove(value)
            else:
                if step.max_select and len(sel) >= step.max_select:
                    return self._t(d, "q.max_select", n=step.max_select)
                sel.append(value)
            a[step.key] = sel
            return None
        if step.kind == "spec":
            return await self._toggle_role(d, value)
        if step.kind == "skills":
            sel = list(a.get("skills") or [])
            names = [s["name"].casefold() for s in sel]
            if value.startswith("free:"):
                name = value[5:]
                if name.casefold() not in names:
                    sel.append({"name": name, "canonical": False, "source": "user_confirmed"})
            elif value.casefold() in names:
                sel = [s for s in sel if s["name"].casefold() != value.casefold()]
            else:
                if len(sel) >= 30:
                    return self._t(d, "q.max_select", n=30)
                sel.append({"name": value, "canonical": True, "source": "user_confirmed"})
            a["skills"] = sel
            return None
        if step.kind == "regions":
            sel = list(a.get("regions") or [])
            sel.remove(value) if value in sel else sel.append(value)
            a["regions"] = sel
            return None
        if step.kind == "cities":
            sel = list(a.get("cities") or [])
            cid = int(value.split("|")[0])
            if any(c["id"] == cid for c in sel):
                sel = [c for c in sel if c["id"] != cid]
            else:
                _, name, country, region = value.split("|")
                sel.append({"id": cid, "name": name, "country": country, "region": region or None})
            a["cities"] = sel
            return None
        return None

    async def _toggle_role(self, d: dict, value: str) -> str | None:
        sel = list(d["a"].get("target_roles") or [])
        if value.startswith("free:"):
            name = value[5:]
            if not any(r.get("free_text") and r["name"].casefold() == name.casefold() for r in sel):
                sel.append({"name": name, "free_text": True})
            d["q"] = None
        else:
            idx = await self.facets.spec_index()
            e = idx.get(value)
            if e is None:
                return None
            if any(r.get("slug") == value for r in sel):
                sel = [r for r in sel if r.get("slug") != value]
            else:
                if len(sel) >= 8:
                    return self._t(d, "q.max_select", n=8)
                sel.append({"name": e.name, "slug": e.slug, "id": e.id, "group": e.group, "is_group": e.is_group})
        d["a"]["target_roles"] = sel
        return None

    def _items(self, d: dict, step: Step) -> list[str]:
        if step.items_from == "core_skills":
            return list(d["a"].get("core_skills") or [])
        return list(d["a"].get(step.items_from) or [])

    # --- rendering ----------------------------------------------------------------------------------------------

    async def render(self, d: dict) -> Screen:
        return (await self._screen(d)).screen

    async def _screen(self, d: dict, notice: str | None = None) -> Outcome:
        step = BY_KEY[d["step"]]
        render = getattr(self, f"_r_{step.kind}")
        screen: Screen = await render(d, step)
        progress = self._progress(d)
        head = f"<b>{progress}</b>\n" if progress and step.kind != "confirm" else ""
        if notice:
            screen.text = f"⚠️ {notice}\n\n{screen.text}"
        screen.text = head + screen.text
        return Outcome("screen", d, screen, notice)

    def _progress(self, d: dict) -> str:
        steps = [s for s in self._flow(d) if applicable(s, d["a"]) and s.kind != "confirm"]
        keys = [s.key for s in steps]
        if d["step"] not in keys:
            return ""
        return self._t(d, "q.progress", i=keys.index(d["step"]) + 1, n=len(keys))

    def _question(self, d: dict, step: Step, **kw) -> str:
        text = self._t(d, f"q.{step.key}", **kw)
        hint = self.tr(d.get("lang") or "ru", f"q.{step.key}.hint", default="")
        return text + (f"\n<i>{hint}</i>" if hint else "")

    def _nav(self, d: dict, step: Step, *, done: bool = False, skip: bool | None = None, any_: bool = False) -> list[list[Button]]:
        row: list[Button] = []
        if d["hist"]:
            row.append(Button(self._t(d, "btn.back"), "qn:b"))
        if skip is None:
            skip = not is_required(step, d["a"])
        if skip:
            row.append(Button(self._t(d, "btn.skip"), "qn:s"))
        if done:
            row.append(Button(self._t(d, "btn.done"), "qn:d"))
        rows = []
        if any_:
            rows.append([Button(self._t(d, "btn.any"), "qn:a")])
        rows.append(row)
        rows.append([Button(self._t(d, "btn.cancel"), "qn:x")])
        return [r for r in rows if r]

    def _grid(self, d: dict, labels: list[tuple[str, str]], per_row: int = 2, paged: bool = True) -> list[list[Button]]:
        """labels: (callback, text). Saves nothing; callers fill d['opts'] first."""
        start = d["page"] * PAGE if paged else 0
        chunk = labels[start: start + PAGE] if paged else labels
        rows: list[list[Button]] = []
        for i in range(0, len(chunk), per_row):
            rows.append([Button(t, cb) for cb, t in chunk[i: i + per_row]])
        if paged and len(labels) > PAGE:
            nav = []
            if d["page"] > 0:
                nav.append(Button("◀️", f"qn:p:{d['page'] - 1}"))
            if start + PAGE < len(labels):
                nav.append(Button("▶️", f"qn:p:{d['page'] + 1}"))
            rows.append(nav)
        return rows

    async def _r_single(self, d: dict, step: Step) -> Screen:
        options = list(step.options)
        d["opts"] = [[v, self._opt(d, step.key, v)] for v in options]
        current = d["a"].get(step.key)
        labels = [(f"qn:o:{i}", ("• " if current == v else "") + lbl) for i, (v, lbl) in enumerate(d["opts"])]
        per_row = 1 if step.key in ("language_flexibility", "search_breadth", "min_score", "notification_mode", "cv", "salary_include_unknown") else 2
        rows = self._grid(d, labels, per_row=per_row)
        return Screen(self._question(d, step), rows + self._nav(d, step), expects_text=step.optional_text)

    async def _r_multi(self, d: dict, step: Step) -> Screen:
        if step.key == "core_skills":
            options = [s["name"] for s in d["a"].get("skills") or []]
            d["opts"] = [[v, v] for v in options]
        elif step.key == "industries":
            groups = await self.facets.specializations()
            d["opts"] = [[g.slug, f"{g.name_de if d['lang'] == 'de' else g.name} ({g.open_jobs})"] for g in groups]
        elif step.key == "countries":
            counts = {c.code: c.open_jobs for c in await self.facets.countries()}
            d["opts"] = [[v, f"{FLAGS.get(v, '')} {self._opt(d, 'country', v)}" + (f" ({counts[v]})" if v in counts else "")] for v in step.options]
        elif step.key == "languages":
            extra = [c for c in d["a"].get("languages") or [] if c not in step.options]
            d["opts"] = [[v, self._opt(d, "lang", v)] for v in [*step.options, *extra]]
        else:
            d["opts"] = [[v, self._opt(d, step.key, v)] for v in step.options]
        sel = d["a"].get(step.key) or []
        labels = [(f"qn:o:{i}", ("✅ " if v in sel else "▫️ ") + lbl) for i, (v, lbl) in enumerate(d["opts"])]
        rows = self._grid(d, labels, per_row=2)
        nav = self._nav(d, step, done=True, any_=step.any_option)
        if step.meta.get("none_option"):
            nav.insert(0, [Button(self._t(d, "btn.none"), "qn:n")])
        return Screen(self._question(d, step, n=step.max_select or ""), rows + nav, expects_text=step.optional_text)

    async def _r_text(self, d: dict, step: Step) -> Screen:
        current = d["a"].get(step.key)
        text = self._question(d, step)
        if current:
            text += "\n\n" + self._t(d, "q.current", value=str(current)[:300])
        return Screen(text, self._nav(d, step), expects_text=True)

    async def _r_per_item(self, d: dict, step: Step) -> Screen:
        items = self._items(d, step)
        if d["item"] >= len(items):
            self._advance(d)
            return (await self._screen(d)).screen
        item = items[d["item"]]
        label = self._opt(d, "lang", item) if step.key == "language_levels" else self._opt(d, "country", item) if step.key == "work_authorization" else item
        d["opts"] = [[v, self._opt(d, step.key, v)] for v in step.item_options]
        labels = [(f"qn:o:{i}", lbl) for i, (_, lbl) in enumerate(d["opts"])]
        rows = self._grid(d, labels, per_row=3 if step.key == "language_levels" else 2, paged=False)
        return Screen(self._question(d, step, item=label, i=d["item"] + 1, n=len(items)), rows + self._nav(d, step))

    async def _r_spec(self, d: dict, step: Step) -> Screen:
        groups = await self.facets.specializations()
        sel = d["a"].get("target_roles") or []
        chosen = ", ".join(r["name"] for r in sel) or "—"
        de = d["lang"] == "de"
        text = self._question(d, step) + "\n\n" + self._t(d, "q.selected", value=chosen)
        if not groups:
            text += "\n\n" + self._t(d, "q.facets_unavailable")
        opts: list[list[str]] = []
        rows: list[list[Button]] = []
        if d.get("q"):
            q = d["q"].casefold()
            for g in groups:
                for c in [g, *(g.children or [])]:
                    if q in c.name.casefold() or q in c.name_de.casefold() or q in c.slug:
                        opts.append([c.slug, f"{c.name_de if de else c.name} ({c.open_jobs})"])
            opts.append([f"free:{d['q']}", self._t(d, "q.free_role", value=d["q"])])
            text += "\n" + self._t(d, "q.search_results", q=d["q"])
        elif d.get("group") is not None:
            g = next((x for x in groups if x.id == d["group"]), None)
            if g:
                opts.append([g.slug, self._t(d, "q.whole_group", name=g.name_de if de else g.name, n=g.open_jobs)])
                for c in g.children or []:
                    opts.append([c.slug, f"{c.name_de if de else c.name} ({c.open_jobs})"])
        d["opts"] = opts
        slugs = {r.get("slug") for r in sel}
        labels = [(f"qn:o:{i}", ("✅ " if v in slugs else "") + lbl) for i, (v, lbl) in enumerate(opts)]
        if opts:
            rows += self._grid(d, labels, per_row=1)
        if d.get("group") is None and not d.get("q"):
            rows += [[Button(f"{g.name_de if de else g.name} ({g.open_jobs}) ›", f"qn:g:{g.id}")] for g in groups]
        else:
            rows.append([Button(self._t(d, "btn.groups"), "qn:G")])
        return Screen(text, rows + self._nav(d, step, done=True), expects_text=True)

    async def _r_skills(self, d: dict, step: Step) -> Screen:
        sel = d["a"].get("skills") or []
        sel_names = {s["name"].casefold() for s in sel}
        if d.get("q"):
            found = await self.facets.skills_search(d["q"], limit=20)
            names = [x.name for x in found]
            opts = [[n, n] for n in names]
            if d["q"].casefold() not in {n.casefold() for n in names}:
                opts.append([f"free:{d['q'][:40]}", self._t(d, "q.free_skill", value=d["q"][:40])])
        else:
            top = await self.facets.skills_top(30)
            names = [s["name"] for s in sel] + [x.name for x in top if x.name.casefold() not in sel_names]
            opts = [[n, n] for n in names]
        d["opts"] = opts
        labels = [(f"qn:o:{i}", ("✅ " if v.casefold() in sel_names else "▫️ ") + lbl) for i, (v, lbl) in enumerate(opts)]
        text = self._question(d, step) + "\n\n" + self._t(d, "q.selected", value=", ".join(s["name"] for s in sel) or "—")
        if d.get("q"):
            text += "\n" + self._t(d, "q.search_results", q=d["q"])
        rows = self._grid(d, labels, per_row=2)
        if d.get("q"):
            rows.append([Button(self._t(d, "btn.top_skills"), "qn:G")])
        return Screen(text, rows + self._nav(d, step, done=True), expects_text=True)

    async def _r_regions(self, d: dict, step: Step) -> Screen:
        opts: list[list[str]] = []
        for country in d["a"].get("countries") or ["DE"]:
            for r in await self.facets.regions(country):
                opts.append([r.code, f"{FLAGS.get(country, '')} {r.name_local} ({r.open_jobs})"])
        d["opts"] = opts
        sel = d["a"].get("regions") or []
        labels = [(f"qn:o:{i}", ("✅ " if v in sel else "") + lbl) for i, (v, lbl) in enumerate(opts)]
        return Screen(self._question(d, step), self._grid(d, labels, per_row=2) + self._nav(d, step, done=True))

    async def _r_cities(self, d: dict, step: Step) -> Screen:
        sel = d["a"].get("cities") or []
        opts = [[f"{c['id']}|{c['name']}|{c['country']}|{c.get('region') or ''}", c["name"]] for c in sel]
        seen = {c["id"] for c in sel}
        regions = set(d["a"].get("regions") or [])
        for country in d["a"].get("countries") or ["DE"]:
            for c in await self.facets.cities(country, q=d.get("q"), limit=15):
                if c.id in seen or (regions and not d.get("q") and c.region not in regions):
                    continue
                seen.add(c.id)
                opts.append([f"{c.id}|{c.city}|{c.country}|{c.region or ''}", f"{FLAGS.get(c.country, '')} {c.city} ({c.open_jobs})"])
        d["opts"] = opts
        ids = {c["id"] for c in sel}
        labels = [(f"qn:o:{i}", ("✅ " if int(v.split("|")[0]) in ids else "") + lbl) for i, (v, lbl) in enumerate(opts)]
        text = self._question(d, step)
        if d.get("q"):
            text += "\n" + self._t(d, "q.search_results", q=d["q"])
        return Screen(text, self._grid(d, labels, per_row=2) + self._nav(d, step, done=True), expects_text=True)

    async def _r_confirm(self, d: dict, step: Step) -> Screen:
        if d.get("q") == "edit":
            d["q"] = None
            keys = [s for s in self._flow(d) if s.kind != "confirm" and applicable(s, d["a"]) and s.key != "ui_language"]
            rows = [[Button(f"{s.num}. {self._t(d, f'label.{s.key}')}", f"qn:e:{s.key}")] for s in keys]
            rows.append([Button(self._t(d, "btn.back_summary"), "qn:p:0")])
            return Screen(self._t(d, "q.edit_which"), rows)
        d["rtc"] = False
        profile = answers_to_profile(d["a"], d["mode"])
        from ..bot.render import profile_summary  # local import: render depends on texts only

        text = profile_summary(profile, d["lang"], mode=d["mode"])
        ok_key = "btn.search_now" if d["mode"] == "manual" else "btn.confirm"
        rows = [
            [Button(self._t(d, ok_key), "qn:ok")],
            [Button(self._t(d, "btn.edit"), "qn:E")],
            [Button(self._t(d, "btn.cancel"), "qn:x")],
        ]
        return Screen(text, rows)

    async def _r_info(self, d: dict, step: Step) -> Screen:  # pragma: no cover - not used currently
        return Screen(self._question(d, step), self._nav(d, step))


def result_profile(d: dict, version: int = 1):
    return answers_to_profile(d["a"], d["mode"], version)


__all__ = ["Engine", "Screen", "Button", "Outcome", "result_profile"]
