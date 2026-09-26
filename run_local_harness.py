#!/usr/bin/env python3
"""
run_local_harness.py — a lightweight stand-in for judge_simulator.py's
context-push + tick flow, but without requiring an LLM API key (it only
exercises the HTTP contract + structural checks, not the LLM scoring).

Does:
  1. healthz + metadata checks
  2. pushes all 5 categories, 50 merchants, 200 customers (warmup, per
     challenge-testing-brief.md Phase 1)
  3. pushes all 100 triggers, then calls /v1/tick with the 30 canonical
     test_pairs.json trigger_ids (in batches, respecting the 20-action cap)
  4. writes submission.jsonl in the exact shape challenge-brief.md §7.2 wants
  5. runs the 3 replay-style scenarios from judge_simulator.py against
     /v1/reply directly (auto-reply hell, intent transition, hostile) as a
     structural sanity check
  6. re-posts one context at an OLDER version to confirm 409 stale_version
     handling, and re-posts the SAME version to confirm the idempotent no-op
"""

import json
import time
import glob
import sys
from pathlib import Path
from urllib import request as urlrequest, error as urlerror

BOT_URL = "http://localhost:8080"
DATASET = Path("expanded")


def call(method, path, body=None, timeout=30):
    url = f"{BOT_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urlrequest.Request(url, data=data, method=method,
                              headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        resp = urlrequest.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode()), (time.time() - t0) * 1000, resp.status
    except urlerror.HTTPError as e:
        return json.loads(e.read().decode()), (time.time() - t0) * 1000, e.code


def load_all(path, key):
    out = {}
    for f in glob.glob(str(DATASET / path / "*.json")):
        d = json.load(open(f))
        out[d[key]] = d
    return out


def main():
    print("=== 1. healthz / metadata ===")
    data, ms, status = call("GET", "/v1/healthz")
    print(f"  healthz: {status} ({ms:.0f}ms) -> {data}")
    data, ms, status = call("GET", "/v1/metadata")
    print(f"  metadata: {status} ({ms:.0f}ms) -> team={data.get('team_name')}, model={data.get('model')}")

    print("\n=== 2. warmup: push categories, merchants, customers ===")
    categories = load_all("categories", "slug")
    merchants = load_all("merchants", "merchant_id")
    customers = load_all("customers", "customer_id")
    triggers = load_all("triggers", "id")

    for slug, payload in categories.items():
        d, ms, status = call("POST", "/v1/context", {
            "scope": "category", "context_id": slug, "version": 1,
            "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})
        assert d.get("accepted"), d
    print(f"  pushed {len(categories)} categories")

    for mid, payload in merchants.items():
        d, ms, status = call("POST", "/v1/context", {
            "scope": "merchant", "context_id": mid, "version": 1,
            "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})
        assert d.get("accepted"), d
    print(f"  pushed {len(merchants)} merchants")

    for cid, payload in customers.items():
        d, ms, status = call("POST", "/v1/context", {
            "scope": "customer", "context_id": cid, "version": 1,
            "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})
        assert d.get("accepted"), d
    print(f"  pushed {len(customers)} customers")

    for tid, payload in triggers.items():
        d, ms, status = call("POST", "/v1/context", {
            "scope": "trigger", "context_id": tid, "version": 1,
            "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})
        assert d.get("accepted"), d
    print(f"  pushed {len(triggers)} triggers")

    data, ms, status = call("GET", "/v1/healthz")
    print(f"  post-warmup healthz -> contexts_loaded={data['contexts_loaded']}")
    assert data["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 100}, \
        "warmup counts don't match the expected 255 base contexts"

    print("\n=== 2b. idempotency + version checks ===")
    d, _, status = call("POST", "/v1/context", {
        "scope": "category", "context_id": "dentists", "version": 1,
        "payload": categories["dentists"], "delivered_at": "2026-04-26T10:00:00Z"})
    print(f"  re-post same version -> accepted={d.get('accepted')} (expect True, no-op)")
    d, _, status = call("POST", "/v1/context", {
        "scope": "category", "context_id": "dentists", "version": 0,
        "payload": categories["dentists"], "delivered_at": "2026-04-26T10:00:00Z"})
    assert status == 409 and not d.get("accepted") and d.get("reason") == "stale_version", (status, d)
    print(f"  re-post STALE version -> HTTP {status}, reason={d.get('reason')} (expect 409/stale_version)")

    print("\n=== 3. tick over the 30 canonical test pairs ===")
    test_pairs = json.load(open(DATASET / "test_pairs.json"))["pairs"]
    trigger_ids = [p["trigger_id"] for p in test_pairs]

    all_actions = []
    batch_size = 15  # stay under the 20-actions-per-tick cap with margin
    for i in range(0, len(trigger_ids), batch_size):
        batch = trigger_ids[i:i + batch_size]
        d, ms, status = call("POST", "/v1/tick", {
            "now": "2026-04-26T10:30:00Z", "available_triggers": batch})
        actions = d.get("actions", [])
        print(f"  batch {i//batch_size + 1}: {len(batch)} triggers -> {len(actions)} actions ({ms:.0f}ms)")
        all_actions.extend(actions)

    # NOTE: 30/30 is the ceiling, not a strict requirement -- a canonical pair
    # whose customer genuinely lacks the consent scope a trigger's kind
    # requires is CORRECTLY declined (no action), by design (see bot.py's
    # CONSENT_BY_TRIGGER_KIND gate). A handful of the generated (non-seed)
    # customers in expanded/ won't have every scope, so a small number of
    # misses here is expected and is the consent gate working, not a bug.
    print(f"\n  total actions returned: {len(all_actions)} (up to 30; a customer-scope pair "
          f"correctly produces no action if that customer lacks the required consent scope)")

    # map trigger_id -> test_id for submission.jsonl
    trg_to_test = {p["trigger_id"]: p["test_id"] for p in test_pairs}

    print("\n=== 4. writing submission.jsonl ===")
    with open("submission.jsonl", "w") as f:
        for a in all_actions:
            test_id = trg_to_test.get(a["trigger_id"], "UNKNOWN")
            line = {
                "test_id": test_id,
                "body": a["body"],
                "cta": a["cta"],
                "send_as": a["send_as"],
                "suppression_key": a["suppression_key"],
                "rationale": a["rationale"],
            }
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(f"  wrote {len(all_actions)} lines to submission.jsonl")

    print("\n=== 5. structural checks on outputs ===")
    issues = 0
    seen_bodies_per_merchant = {}
    for a in all_actions:
        if not a.get("body", "").strip():
            print(f"  ISSUE: empty body for {a['trigger_id']}"); issues += 1
        if a.get("cta") not in ("binary", "open_ended", "none"):
            print(f"  ISSUE: bad cta '{a.get('cta')}' for {a['trigger_id']}"); issues += 1
        if a.get("send_as") not in ("vera", "merchant_on_behalf"):
            print(f"  ISSUE: bad send_as '{a.get('send_as')}' for {a['trigger_id']}"); issues += 1
        mid = a["merchant_id"]
        seen_bodies_per_merchant.setdefault(mid, []).append(a["body"])
    dup_merchants = {m: b for m, b in seen_bodies_per_merchant.items() if len(b) != len(set(b))}
    print(f"  merchants with verbatim-repeated bodies across their actions: {len(dup_merchants)}")
    print(f"  total structural issues: {issues}")

    print("\n=== 6. replay-style scenario checks against /v1/reply ===")
    # --- auto-reply hell ---
    mid = test_pairs[0]["merchant_id"]
    conv = "conv_replay_autoreply"
    auto_msg = "Thank you for contacting us! Our team will respond shortly."
    outcomes = []
    for turn in range(1, 5):
        d, ms, status = call("POST", "/v1/reply", {
            "conversation_id": conv, "merchant_id": mid, "customer_id": None,
            "from_role": "merchant", "message": auto_msg,
            "received_at": "2026-04-26T11:00:00Z", "turn_number": turn})
        outcomes.append(d.get("action"))
        if d.get("action") == "end":
            break
    print(f"  auto-reply-hell action sequence: {outcomes} (expect to end by turn 2-3)")

    # --- intent transition ---
    conv2 = "conv_replay_intent"
    call("POST", "/v1/reply", {"conversation_id": conv2, "merchant_id": mid, "customer_id": None,
                                 "from_role": "merchant", "message": "Tell me more about this",
                                 "received_at": "2026-04-26T11:00:00Z", "turn_number": 1})
    d, ms, status = call("POST", "/v1/reply", {
        "conversation_id": conv2, "merchant_id": mid, "customer_id": None,
        "from_role": "merchant", "message": "Ok lets do it. Whats next?",
        "received_at": "2026-04-26T11:05:00Z", "turn_number": 2})
    body = d.get("body", "")
    qualifying = any(w in body.lower() for w in ["would you", "do you want", "are you interested"])
    print(f"  intent-transition body: \"{body}\"")
    print(f"  still qualifying after commitment? {qualifying} (expect False)")

    # --- hostile ---
    conv3 = "conv_replay_hostile"
    d, ms, status = call("POST", "/v1/reply", {
        "conversation_id": conv3, "merchant_id": mid, "customer_id": None,
        "from_role": "merchant", "message": "Stop messaging me. This is useless spam.",
        "received_at": "2026-04-26T11:00:00Z", "turn_number": 1})
    print(f"  hostile-message action: {d.get('action')} (expect 'end')")

    print("\n=== DONE ===")


if __name__ == "__main__":
    main()
