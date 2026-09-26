#!/usr/bin/env python3
"""
bot.py — magicpin AI Challenge submission ("Vera 2.0")

Implements the 5 endpoints defined in challenge-testing-brief.md §2:
    POST /v1/context    receive a context push (idempotent by version)
    POST /v1/tick       periodic wake-up; bot may initiate up to 20 actions
    POST /v1/reply      receive a merchant/customer reply; bot must respond
    GET  /v1/healthz    liveness probe
    GET  /v1/metadata   bot identity

Architecture (see README.md for the full writeup):
    contexts.py               safe accessors over raw context dicts
    composer.py                deterministic compose(category, merchant,
                                trigger, customer?) — the message engine
    conversation_handlers.py  respond(state, merchant_message) — multi-turn
    bot.py (this file)        HTTP surface + in-memory state store

State is entirely in-memory (per the testing brief: "Storing in memory is
fine; just don't restart between calls"). Run with:
    uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from composer import compose
from conversation_handlers import respond as conv_respond, new_state, is_auto_reply_message, _normalize
from contexts import customer_has_consent, merchant_display_name, parse_iso_date

START_TIME = time.time()
TICK_ACTION_CAP = 20  # challenge-testing-brief.md §5

app = FastAPI(title="Vera 2.0 — magicpin AI Challenge bot")

# ---------------------------------------------------------------------------
# in-memory stores
# ---------------------------------------------------------------------------

# (scope, context_id) -> {"version": int, "payload": dict}
CONTEXTS: dict[tuple[str, str], dict[str, Any]] = {}

# conversation_id -> conversation_handlers state dict
CONVERSATIONS: dict[str, dict[str, Any]] = {}

# suppression_key -> last-sent unix ts, for basic trigger-level dedup
# (challenge-brief.md §4.3: suppression_key exists "for dedup")
SUPPRESSION_LOG: dict[str, float] = {}
SUPPRESSION_TTL_SECONDS = 24 * 3600  # don't re-fire the same suppression_key within 24h

# trigger_id -> set of (merchant_id, customer_id) already actioned, so a tick
# never re-proposes a brand new conversation for a trigger already handled
TRIGGERED_ALREADY: set[tuple[str, str]] = set()

# merchant/message -> count.  The official auto-reply replay deliberately
# changes conversation_id between repeated WA-Business replies.
AUTO_REPLY_FINGERPRINTS: dict[tuple[str, str], int] = {}

CONSENT_BY_TRIGGER_KIND = {
    "recall_due": "recall_reminders",
    "appointment_tomorrow": "appointment_reminders",
    "chronic_refill_due": ("refill_reminders", "delivery_notifications"),
    "customer_lapsed_soft": ("winback_offers", "promotional_offers"),
    "customer_lapsed_hard": ("winback_offers", "promotional_offers"),
    "trial_followup": ("kids_program_updates", "program_updates", "treatment_followup"),
    "wedding_package_followup": ("bridal_package_followup", "promotional_offers"),
}


def _get_ctx(scope: str, context_id: str) -> Optional[dict]:
    entry = CONTEXTS.get((scope, context_id))
    return entry["payload"] if entry else None


def _category_for_merchant(merchant: dict) -> Optional[dict]:
    slug = merchant.get("category_slug")
    return _get_ctx("category", slug) if slug else None


def _counts_by_scope() -> dict[str, int]:
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in CONTEXTS.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# GET /v1/healthz
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": _counts_by_scope(),
    }


# ---------------------------------------------------------------------------
# GET /v1/metadata
# ---------------------------------------------------------------------------

@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vera 2.0",
        "team_members": ["Divyansh Purohit"],
        "model": "deterministic-template-composer-v1 (no LLM in the hot path)",
        "approach": (
            "compose() dispatches by trigger.kind into ~25 family templates, each "
            "grounded directly in the 4 context dicts with a graceful degrade path "
            "when payloads are placeholders/sparse (generated, non-seed merchants "
            "and ~40% of generated triggers). No LLM call in /v1/tick or /v1/reply "
            "hot paths -> deterministic, fast, zero hallucination risk by "
            "construction. See README.md for full rationale."
        ),
        "contact_email": "purohitdivyansh302@gmail.com",
        "version": "1.0.0",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# POST /v1/context
# ---------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


VALID_SCOPES = {"category", "merchant", "customer", "trigger"}


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in VALID_SCOPES:
        return JSONResponse(status_code=400, content={
            "accepted": False,
            "reason": "invalid_scope",
            "details": f"scope must be one of {sorted(VALID_SCOPES)}",
        })

    key = (body.scope, body.context_id)
    cur = CONTEXTS.get(key)

    if cur and cur["version"] == body.version:
        # idempotent no-op re-post of the same version
        return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}",
                "stored_at": datetime.now(timezone.utc).isoformat()}

    if cur and cur["version"] > body.version:
        return JSONResponse(status_code=409, content={
            "accepted": False, "reason": "stale_version", "current_version": cur["version"],
        })

    CONTEXTS[key] = {"version": body.version, "payload": body.payload}
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}",
            "stored_at": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# POST /v1/tick
# ---------------------------------------------------------------------------

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


def _suppressed(suppression_key: str, now_ts: float) -> bool:
    last = SUPPRESSION_LOG.get(suppression_key)
    return bool(last) and (now_ts - last) < SUPPRESSION_TTL_SECONDS


def _tick_time(value: str) -> datetime:
    """Parse the caller's clock once so expiry and suppression are reproducible."""
    parsed = parse_iso_date(value)
    if parsed is None:
        raise HTTPException(status_code=422, detail="now must be a valid ISO-8601 timestamp")
    return parsed


@app.post("/v1/tick")
async def tick(body: TickBody):
    now = _tick_time(body.now)
    now_ts = now.timestamp()
    actions: list[dict] = []

    for trigger_id in body.available_triggers:
        if len(actions) >= TICK_ACTION_CAP:
            break

        trigger = _get_ctx("trigger", trigger_id)
        if not trigger:
            continue  # we were never pushed this trigger's payload; skip silently

        expires_at = parse_iso_date(trigger.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            continue  # the action is no longer timely; never send stale outreach

        merchant_id = trigger.get("merchant_id")
        customer_id = trigger.get("customer_id")
        merchant = _get_ctx("merchant", merchant_id) if merchant_id else None
        if not merchant:
            continue  # can't compose without the merchant we'd be writing about

        category = _category_for_merchant(merchant)
        if not category:
            continue  # no category context yet — restraint over a voiceless guess

        customer = _get_ctx("customer", customer_id) if customer_id else None
        if trigger.get("scope") == "customer" and not customer:
            continue  # customer-scope trigger with no customer context pushed yet
        if trigger.get("scope") == "customer":
            consent_scope = CONSENT_BY_TRIGGER_KIND.get(trigger.get("kind"))
            if not customer_has_consent(customer, consent_scope):
                continue  # customer exists, but has not opted into this outreach purpose

        suppression_key = trigger.get("suppression_key") or f"{trigger_id}:{merchant_id}"
        if _suppressed(suppression_key, now_ts):
            continue  # already sent this exact thing recently — restraint is rewarded

        dedup_key = (merchant_id, customer_id or "")
        conv_key = f"{trigger_id}:{dedup_key[0]}:{dedup_key[1]}"
        if conv_key in TRIGGERED_ALREADY:
            continue

        composed = compose(category, merchant, trigger, customer)

        conversation_id = f"conv_{merchant_id}_{trigger_id}_{uuid.uuid4().hex[:6]}"
        display_name = merchant_display_name(merchant)

        state = new_state(merchant_id=merchant_id, customer_id=customer_id,
                           merchant_name=display_name, first_body=composed["body"])
        CONVERSATIONS[conversation_id] = state

        SUPPRESSION_LOG[suppression_key] = now_ts
        TRIGGERED_ALREADY.add(conv_key)

        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trigger_id,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
            "template_params": composed.get("template_params", []),
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": suppression_key,
            "rationale": composed["rationale"],
        })

    return {"actions": actions}


# ---------------------------------------------------------------------------
# POST /v1/reply
# ---------------------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    state = CONVERSATIONS.get(body.conversation_id)
    if state is None:
        # judge replayed a conversation we don't have local state for (e.g.
        # a fresh replay-test scenario) — bootstrap minimal state so we can
        # still respond sensibly rather than erroring out.
        merchant = _get_ctx("merchant", body.merchant_id) if body.merchant_id else None
        display_name = merchant_display_name(merchant) if merchant else "there"
        state = new_state(merchant_id=body.merchant_id, customer_id=body.customer_id,
                           merchant_name=display_name)
        CONVERSATIONS[body.conversation_id] = state

    if is_auto_reply_message(body.message):
        fingerprint = (body.merchant_id or "", _normalize(body.message))
        AUTO_REPLY_FINGERPRINTS[fingerprint] = AUTO_REPLY_FINGERPRINTS.get(fingerprint, 0) + 1
        if AUTO_REPLY_FINGERPRINTS[fingerprint] >= 2:
            state["ended"] = True
            state["mode"] = "ended"
            return {
                "action": "end",
                "rationale": "Repeated WA-Business auto-reply detected across conversation ids; ending instead of spending another turn on an automated responder.",
            }

    result = conv_respond(state, body.message)

    # anti-repetition guard (challenge-testing-brief.md §10 penalty: -2 per
    # verbatim repeat). If the handler produced something we've already sent
    # in this conversation, fall back to a short, honest, non-repetitive nudge.
    if result.get("action") == "send":
        sent_before = state.get("sent_bodies", [])[:-1]  # exclude the one just recorded
        if result["body"] in sent_before:
            result["body"] = "Just circling back on this — still worth a look when you get a sec?"
            result["rationale"] += " [anti-repetition guard: original response matched a prior send verbatim, substituted a short honest circle-back instead]"

    return result


# ---------------------------------------------------------------------------
# optional: teardown (challenge-testing-brief.md §11 — wipe state at test end)
# ---------------------------------------------------------------------------

@app.post("/v1/teardown")
async def teardown():
    CONTEXTS.clear()
    CONVERSATIONS.clear()
    SUPPRESSION_LOG.clear()
    TRIGGERED_ALREADY.clear()
    AUTO_REPLY_FINGERPRINTS.clear()
    return {"status": "wiped"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
