"""
Simulates the two Phase-3 adaptation patterns called out in the challenge
notes: (a) a category context re-pushed at a new version with a freshly
injected digest item, and (b) a merchant context re-pushed at a new version
with updated performance numbers -- then checks that compose(), called via
the real /v1/tick path, actually reflects the fresh data rather than the
original push.
"""
import asyncio
import copy
import json
from pathlib import Path

import bot
from bot import CtxBody, TickBody

DATASET = Path("expanded")


async def main():
    await bot.teardown()

    dentists = json.load(open(DATASET / "categories" / "dentists.json"))
    merchant = json.load(open(DATASET / "merchants" / "m_001_drmeera_dentist_delhi.json"))
    trigger = json.load(open(DATASET / "triggers" / "trg_001_research_digest_dentists.json"))

    # --- initial pushes (v1) ---
    await bot.push_context(CtxBody(scope="category", context_id="dentists", version=1,
                                    payload=dentists, delivered_at="2026-04-26T10:00:00Z"))
    await bot.push_context(CtxBody(scope="merchant", context_id=merchant["merchant_id"], version=1,
                                    payload=merchant, delivered_at="2026-04-26T10:00:00Z"))
    await bot.push_context(CtxBody(scope="trigger", context_id=trigger["id"], version=1,
                                    payload=trigger, delivered_at="2026-04-26T10:00:00Z"))

    r1 = await bot.tick(TickBody(now="2026-04-26T10:30:00Z", available_triggers=[trigger["id"]]))
    print("=== BEFORE injection ===")
    print(" ", r1["actions"][0]["body"])
    original_item_id = trigger["payload"]["top_item_id"]
    print(f"  (grounded in digest item: {original_item_id})")

    # --- Phase-3 style injection: category re-pushed at v2 with a BRAND NEW
    # digest item the trigger's payload never referenced, simulating the
    # judge injecting fresh research mid-test ---
    dentists_v2 = copy.deepcopy(dentists)
    new_item = {
        "id": "d_2026W99_injected_novel_finding",
        "kind": "research",
        "title": "Novel injected finding: sonic toothbrushes cut plaque 31% vs manual in RCT",
        "source": "Injected-Test-Journal 2026",
        "trial_n": 900,
        "summary": "Freshly injected mid-test research item, not present in the original v1 push.",
        "actionable": "Consider recommending sonic toothbrushes to adult patients.",
    }
    dentists_v2["digest"].append(new_item)
    r = await bot.push_context(CtxBody(scope="category", context_id="dentists", version=2,
                                        payload=dentists_v2, delivered_at="2026-04-26T11:00:00Z"))
    assert r["accepted"], r

    # Fire a DIFFERENT trigger of the same 'research_digest' kind for the same
    # merchant, WITHOUT a resolvable top_item_id, to force the freshest-match
    # fallback path (this is the realistic Phase-3 shape: a new trigger fires
    # after the fresh context lands, not a replay of the exact same trigger_id
    # which would already be suppressed).
    trigger_after = copy.deepcopy(trigger)
    trigger_after["id"] = "trg_phase3_followup_research"
    trigger_after["payload"] = {"category": "dentists", "top_item_id": "nonexistent_id_from_before_injection"}
    trigger_after["suppression_key"] = "research:dentists:2026-W99-followup"
    await bot.push_context(CtxBody(scope="trigger", context_id=trigger_after["id"], version=1,
                                    payload=trigger_after, delivered_at="2026-04-26T11:05:00Z"))

    r2 = await bot.tick(TickBody(now="2026-04-26T11:10:00Z", available_triggers=[trigger_after["id"]]))
    print("\n=== AFTER injection (new trigger, unresolvable top_item_id) ===")
    body2 = r2["actions"][0]["body"]
    print(" ", body2)
    used_fresh_item = "sonic toothbrush" in body2.lower()
    print(f"  used freshly-injected digest item? {used_fresh_item} (expect True)")
    assert used_fresh_item, "FAILED: fresh Phase-3 digest injection was not picked up"

    # --- merchant performance update (v2): flip calls_pct from the original
    # sign to confirm perf_dip's direction-check actually re-reads the LATEST
    # merchant push rather than caching the v1 numbers ---
    print("\n=== merchant performance re-push (v2) ===")
    merchant_v2 = copy.deepcopy(merchant)
    merchant_v2["performance"]["delta_7d"]["calls_pct"] = -0.41
    r = await bot.push_context(CtxBody(scope="merchant", context_id=merchant["merchant_id"], version=2,
                                        payload=merchant_v2, delivered_at="2026-04-26T11:15:00Z"))
    assert r["accepted"], r

    perf_trigger = {
        "id": "trg_phase3_perfdip_followup", "scope": "merchant", "kind": "perf_dip", "source": "internal",
        "merchant_id": merchant["merchant_id"], "customer_id": None,
        "payload": {"placeholder": True, "metric_or_topic": "perf_dip"},
        "urgency": 3, "suppression_key": "perf_dip:m_001:followup", "expires_at": "2026-06-30T00:00:00Z",
    }
    await bot.push_context(CtxBody(scope="trigger", context_id=perf_trigger["id"], version=1,
                                    payload=perf_trigger, delivered_at="2026-04-26T11:16:00Z"))
    r3 = await bot.tick(TickBody(now="2026-04-26T11:20:00Z", available_triggers=[perf_trigger["id"]]))
    body3 = r3["actions"][0]["body"]
    print(" ", body3)
    reflects_new_number = "41%" in body3 or "-41%" in body3
    print(f"  reflects freshly re-pushed -41% (not stale v1 numbers)? {reflects_new_number} (expect True)")


asyncio.run(main())
