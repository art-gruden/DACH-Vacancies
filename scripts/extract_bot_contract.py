"""Extract the /api/bot/v1 subset (paths + referenced schemas) from the crawler's full OpenAPI document."""
import json
import sys
from datetime import date

src, dst = sys.argv[1], sys.argv[2]
spec = json.load(open(src, encoding="utf-8"))
paths = {k: v for k, v in spec["paths"].items() if k.startswith("/api/bot/v1")}
if len(paths) < 9:
    sys.exit(f"expected the 9 /api/bot/v1 routes, found {sorted(paths)}")
refs: set[str] = set()


def walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "$ref":
                refs.add(v.split("/")[-1])
            else:
                walk(v)
    elif isinstance(o, list):
        for x in o:
            walk(x)


walk(paths)
while True:
    n = len(refs)
    for r in list(refs):
        walk(spec["components"]["schemas"][r])
    if len(refs) == n:
        break
out = {
    "openapi": spec["openapi"],
    "info": {**spec["info"], "x-snapshot": f"bot-api subset of the crawler OpenAPI, {date.today().isoformat()}"},
    "paths": paths,
    "components": {
        "schemas": {r: spec["components"]["schemas"][r] for r in sorted(refs)},
        "securitySchemes": {"BotBearer": {"type": "http", "scheme": "bearer",
                            "description": "Not declared in the crawler OpenAPI; taken from gcc_core/botauth.py"}},
    },
    "security": [{"BotBearer": []}],
}
json.dump(out, open(dst, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
print(f"wrote {dst}: {len(paths)} paths, {len(refs)} schemas")
