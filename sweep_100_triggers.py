import glob, json, re
from pathlib import Path
from composer import compose
from contexts import violates_taboo

DATASET = Path("expanded")

def load_all(path, key):
    out = {}
    for f in glob.glob(str(DATASET / path / "*.json")):
        d = json.load(open(f))
        out[d[key]] = d
    return out

categories = load_all("categories", "slug")
merchants = load_all("merchants", "merchant_id")
customers = load_all("customers", "customer_id")
triggers = load_all("triggers", "id")

# raw internal kind/jargon tokens that should never leak into merchant-facing body
KIND_NAMES = {t.get("kind") for t in triggers.values()}
JARGON_TOKENS = ["placeholder", "metric_or_topic", "suppression_key", "trigger_id",
                 "gbp ", "gbp.", "gbp,", "gbp)", " gbp"]  # raw GBP acronym per README's own audit

issues = []
n_composed = 0
n_crash = 0
empty = 0
multi_cta = 0
taboo_leaks = 0
kind_leaks = 0
jargon_leaks = 0

for tid, trigger in sorted(triggers.items()):
    merchant = merchants.get(trigger.get("merchant_id"))
    if not merchant:
        continue
    category = categories.get(merchant.get("category_slug"))
    if not category:
        continue
    customer = customers.get(trigger.get("customer_id")) if trigger.get("customer_id") else None

    try:
        result = compose(category, merchant, trigger, customer)
    except Exception as e:
        n_crash += 1
        issues.append(f"CRASH on {tid}: {e}")
        continue
    n_composed += 1
    body = result.get("body", "")

    if not body.strip():
        empty += 1
        issues.append(f"EMPTY body: {tid}")

    if body.count("?") > 1:
        multi_cta += 1
        issues.append(f"MULTI-CTA ({body.count('?')} '?'): {tid}: {body!r}")

    bad = violates_taboo(body, category)
    if bad:
        taboo_leaks += 1
        issues.append(f"TABOO leak {bad}: {tid}: {body!r}")

    low = body.lower()
    kind = trigger.get("kind", "")
    if kind and kind.replace("_", " ") in low:
        kind_leaks += 1
        issues.append(f"KIND-NAME leak '{kind}': {tid}: {body!r}")

    for jt in JARGON_TOKENS:
        if jt.strip().lower() in low:
            jargon_leaks += 1
            issues.append(f"JARGON token '{jt.strip()}': {tid}: {body!r}")

    if result.get("cta") not in ("binary", "open_ended", "none"):
        issues.append(f"BAD CTA '{result.get('cta')}': {tid}")

    if len(body) > 700:
        issues.append(f"OVER LENGTH ({len(body)} chars): {tid}")

print(f"Composed {n_composed}/{len(triggers)} triggers without crashing")
print(f"  crashes:      {n_crash}")
print(f"  empty bodies: {empty}")
print(f"  multi-CTA:    {multi_cta}")
print(f"  taboo leaks:  {taboo_leaks}")
print(f"  kind-name leaks (raw enum phrase surfaced): {kind_leaks}")
print(f"  jargon-token leaks: {jargon_leaks}")
print(f"\n{len(issues)} total issue lines:")
for i in issues[:60]:
    print(" -", i)
