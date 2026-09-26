import asyncio, json
from pathlib import Path
import bot
from bot import CtxBody, TickBody
from contexts import customer_has_consent, parse_iso_date

DATASET = Path("expanded")

def load_all(path, key):
    import glob
    out = {}
    for f in glob.glob(str(DATASET / path / "*.json")):
        d = json.load(open(f))
        out[d[key]] = d
    return out

async def main():
    await bot.teardown()
    categories = load_all("categories", "slug")
    merchants = load_all("merchants", "merchant_id")
    customers = load_all("customers", "customer_id")
    triggers = load_all("triggers", "id")

    for slug, payload in categories.items():
        await bot.push_context(CtxBody(scope="category", context_id=slug, version=1, payload=payload, delivered_at="2026-04-26T10:00:00Z"))
    for mid, payload in merchants.items():
        await bot.push_context(CtxBody(scope="merchant", context_id=mid, version=1, payload=payload, delivered_at="2026-04-26T10:00:00Z"))
    for cid, payload in customers.items():
        await bot.push_context(CtxBody(scope="customer", context_id=cid, version=1, payload=payload, delivered_at="2026-04-26T10:00:00Z"))
    for tid, payload in triggers.items():
        await bot.push_context(CtxBody(scope="trigger", context_id=tid, version=1, payload=payload, delivered_at="2026-04-26T10:00:00Z"))

    test_pairs = json.load(open(DATASET / "test_pairs.json"))["pairs"]
    trigger_ids = [p["trigger_id"] for p in test_pairs]
    # batch at 15 (as run_local_harness.py / the sandbox harness do) so the
    # 20-actions-per-tick cap doesn't get confused with a genuine miss
    fired = set()
    for i in range(0, len(trigger_ids), 15):
        d = await bot.tick(TickBody(now="2026-04-26T10:30:00Z", available_triggers=trigger_ids[i:i+15]))
        fired |= {a["trigger_id"] for a in d["actions"]}
    missing = [tid for tid in trigger_ids if tid not in fired]
    print(f"{len(missing)} missing of {len(trigger_ids)}\n")

    now = parse_iso_date("2026-04-26T10:30:00Z")
    for tid in missing:
        t = triggers.get(tid)
        print(f"--- {tid} ---")
        if not t:
            print("  NOT PUSHED / not found in trigger store"); continue
        print(f"  kind={t.get('kind')} scope={t.get('scope')} merchant_id={t.get('merchant_id')} customer_id={t.get('customer_id')}")
        exp = parse_iso_date(t.get("expires_at"))
        print(f"  expires_at={t.get('expires_at')} parsed={exp} now={now} expired={exp is not None and exp <= now}")
        m = merchants.get(t.get("merchant_id"))
        print(f"  merchant found: {bool(m)}  category_slug={m.get('category_slug') if m else None}")
        cat = categories.get(m.get("category_slug")) if m else None
        print(f"  category found: {bool(cat)}")
        cust = customers.get(t.get("customer_id")) if t.get("customer_id") else None
        print(f"  customer present in dataset: {bool(cust)} (customer_id={t.get('customer_id')})")
        if t.get("scope") == "customer":
            consent_scope = bot.CONSENT_BY_TRIGGER_KIND.get(t.get("kind"))
            has_consent = customer_has_consent(cust, consent_scope)
            print(f"  required consent_scope={consent_scope}  customer.consent.scope={cust.get('consent',{}).get('scope') if cust else None}  has_consent={has_consent}")

asyncio.run(main())
