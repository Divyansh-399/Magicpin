"""
conversation_handlers.py — respond(state, merchant_message) -> dict

Handles the three replay scenarios called out explicitly in
challenge-testing-brief.md §4 Phase 4:
  1. Auto-reply hell     — same canned WA-Business auto-reply repeated 3-4x
  2. Intent transition   — merchant commits ("ok let's do it") after
                            qualification; must switch to ACTION, not re-ask
  3. Hostile / off-topic — abuse, then an unrelated ask; stay on-mission,
                            polite, don't over-apologize into a loop

Also implements the two other open challenges from challenge-brief.md §12:
  4. graceful stop after repeated unanswered nudges / explicit "not interested"
  5. per-turn language detection (rough) so a Hindi reply gets a Hindi-leaning
     response even if the merchant's profile default is English

ConversationState is a plain dict so bot.py can serialize it trivially;
this module only reads/mutates that dict, it never touches the context store.
"""

from __future__ import annotations
import re
from typing import Any, Optional

# ---------------------------------------------------------------------------
# constants / heuristics
# ---------------------------------------------------------------------------

# Canned WhatsApp Business auto-reply phrasing (English + common Hindi/Hinglish
# variants seen in challenge-brief.md §9 Pattern B and general WA Business
# defaults). Not exhaustive by design — combined with the repeat-detector
# below, which is the more reliable signal per the brief's own hint:
# "same message verbatim 3+ times = auto-reply".
AUTO_REPLY_PHRASES = [
    "thank you for contacting", "thanks for contacting", "we will get back to you",
    "will respond shortly", "automated assistant", "currently unavailable",
    "aapki jaankari ke liye", "team tak pahunchane", "hamari team tak",
    "automated reply", "this is an automated", "we'll reply soon",
    "out of office", "busy right now",
]

HOSTILE_PHRASES = [
    "stop messaging", "spam", "fuck", "useless", "shut up", "scam",
    "harass", "block you", "reported", "bakwas", "bewakoof",
]

NOT_INTERESTED_PHRASES = [
    "not interested", "no thanks", "nahi chahiye", "don't message",
    "leave me alone", "unsubscribe", "remove me",
]

COMMIT_PHRASES = [
    "ok lets do it", "ok let's do it", "let's do it", "lets do it", "go ahead",
    "yes lets", "yes let's", "sounds good", "sure go ahead", "proceed",
    "confirm", "yes please do", "haan kar do", "theek hai kar do", "kar do",
    "chalo karte hain",
]

QUALIFYING_STARTERS = [
    "would you", "do you want", "are you interested", "can you tell",
    "what if", "how about", "would it help", "kya aap", "chahiye kya",
]

OFF_TOPIC_HINTS = [
    "gst", "income tax", "loan", "visa", "legal advice", "personal problem",
]

HINDI_HINT_CHARS = re.compile(r"[\u0900-\u097F]")  # Devanagari block
HINGLISH_WORDS = {"hai", "kya", "nahi", "aap", "kar", "karo", "chahiye", "theek", "haan", "bhai", "yaar"}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _looks_hindi_ish(text: str) -> bool:
    low = text.lower()
    if HINDI_HINT_CHARS.search(text):
        return True
    words = set(re.findall(r"[a-z]+", low))
    return len(words & HINGLISH_WORDS) >= 1


def _contains_any(text: str, phrases: list[str]) -> bool:
    low = text.lower()
    return any(p in low for p in phrases)


def is_auto_reply_message(text: str) -> bool:
    """Public, deterministic auto-reply classifier used across conversations.

    The official replay may issue each repeated WA-Business reply under a new
    conversation id, so per-conversation state alone is not enough.
    """
    return _contains_any(_normalize(text), AUTO_REPLY_PHRASES)


# ---------------------------------------------------------------------------
# state helpers — state is a plain dict, created/owned by bot.py per conversation_id
# ---------------------------------------------------------------------------

def new_state(merchant_id: Optional[str] = None, customer_id: Optional[str] = None,
              merchant_name: str = "there", first_body: str = "") -> dict:
    return {
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "merchant_name": merchant_name,
        "sent_bodies": [first_body] if first_body else [],
        "incoming_history": [],     # list of normalized incoming messages
        "consecutive_auto_replies": 0,
        "consecutive_unanswered": 0,   # bumped by bot.py when a `wait` times out without a reply
        "turns": 0,
        "ended": False,
        "mode": "pitch",            # "pitch" | "action" | "ended"
    }


# ---------------------------------------------------------------------------
# core entry point
# ---------------------------------------------------------------------------

def respond(state: dict, merchant_message: str) -> dict:
    """Given conversation-so-far `state` (mutated in place) and the latest
    incoming message, decide: send / wait / end."""
    state["turns"] = state.get("turns", 0) + 1
    norm = _normalize(merchant_message)
    state.setdefault("incoming_history", []).append(norm)

    if state.get("ended"):
        return {"action": "end", "rationale": "Conversation already ended; ignoring further input."}

    # --- 1. auto-reply detection (verbatim repeat OR canned-phrase match) ---
    is_repeat = state["incoming_history"].count(norm) >= 2  # this message seen before
    is_canned_phrase = _contains_any(norm, AUTO_REPLY_PHRASES)

    if is_repeat or is_canned_phrase:
        state["consecutive_auto_replies"] = state.get("consecutive_auto_replies", 0) + 1
    else:
        state["consecutive_auto_replies"] = 0

    if state["consecutive_auto_replies"] >= 2:
        # per challenge-brief.md Pattern B: try ONE redirect after first
        # detection, then exit gracefully rather than burning more turns.
        state["ended"] = True
        state["mode"] = "ended"
        return {
            "action": "end",
            "rationale": (f"Detected {state['consecutive_auto_replies']} consecutive auto-reply-shaped "
                          "messages (repeat-verbatim or canned WA-Business phrasing) after already "
                          "attempting one human redirect. Exiting to avoid burning further turns, "
                          "per the auto-reply anti-pattern in challenge-brief.md §3."),
        }
    if state["consecutive_auto_replies"] == 1:
        body = _auto_reply_redirect(state)
        _record_sent(state, body)
        return {"action": "send", "body": body, "cta": "binary",
                "rationale": "First auto-reply-shaped message detected — trying one direct, low-effort redirect to reach a human before giving up (mirrors challenge-brief.md Pattern B)."}

    # --- 2. hostile detection ---
    if _contains_any(norm, HOSTILE_PHRASES):
        state["ended"] = True
        state["mode"] = "ended"
        body = _hostile_graceful_exit(state)
        # brief's own scoring example accepts either a graceful "end" or an
        # apologetic "send" that stops the ask — we choose "end" with a warm
        # sign-off so the bot doesn't keep the thread open after abuse, while
        # still being able to re-engage later via a fresh trigger.
        return {"action": "end",
                "rationale": "Hostile/abusive message detected. Exiting the conversation politely rather than re-pitching or arguing; a future trigger can re-open contact later. No apology-loop, no defensiveness."}

    # --- 3. not-interested / opt-out ---
    if _contains_any(norm, NOT_INTERESTED_PHRASES):
        state["ended"] = True
        state["mode"] = "ended"
        return {"action": "end",
                "rationale": "Explicit not-interested/opt-out signal — graceful exit per challenge-brief.md §12.5 ('knowing when to stop')."}

    # --- 4. too many unanswered nudges already (set externally by bot.py on timeouts) ---
    if state.get("consecutive_unanswered", 0) >= 3:
        state["ended"] = True
        state["mode"] = "ended"
        return {"action": "end",
                "rationale": "3 consecutive nudges went unanswered — stopping rather than continuing to push, per challenge-brief.md §12.5."}

    # --- 5. off-topic but not hostile: acknowledge briefly, redirect to mission ---
    if _contains_any(norm, OFF_TOPIC_HINTS) and not _contains_any(norm, COMMIT_PHRASES):
        body = _off_topic_redirect(state)
        _record_sent(state, body)
        return {"action": "send", "body": body, "cta": "open_ended",
                "rationale": "Off-mission question detected. One-line honest boundary + redirect back to the original thread, staying polite and on-mission without ignoring the merchant."}

    # --- 6. intent / commitment transition: switch pitch -> action immediately ---
    if _contains_any(norm, COMMIT_PHRASES) or state.get("mode") == "action":
        state["mode"] = "action"
        body = _action_mode_response(state, merchant_message)
        _record_sent(state, body)
        return {"action": "send", "body": body, "cta": "open_ended",
                "rationale": ("Merchant signaled explicit commitment — switching straight to ACTION "
                              "language ('done'/'sending'/'here's the draft'), never re-asking a "
                              "qualifying question. This is the exact failure mode challenge-brief.md "
                              "Pattern D calls out.")}

    # --- 7. default: acknowledge + advance the pitch by one concrete step ---
    body = _pitch_mode_response(state, merchant_message)
    _record_sent(state, body)
    return {"action": "send", "body": body, "cta": "open_ended",
            "rationale": "Neutral/engaged reply — advancing the conversation with one concrete next step, still in pitch mode since no explicit commitment or objection was detected."}


def _record_sent(state: dict, body: str) -> None:
    state.setdefault("sent_bodies", []).append(body)


# ---------------------------------------------------------------------------
# response builders
# ---------------------------------------------------------------------------

def _lang_pack(state: dict, merchant_message: str):
    """Very rough per-turn language detection (challenge-brief.md §12.4):
    if this specific incoming message looks Hindi/Hinglish, mirror that in
    the reply even if the stored default is English."""
    return _looks_hindi_ish(merchant_message)


def _auto_reply_redirect(state: dict) -> str:
    name = state.get("merchant_name", "there")
    return (f"Samajh gayi — but this one's quick and needs your OK directly, "
            f"not the front-desk. 2 minutes, want to just glance at it yourself?")


def _hostile_graceful_exit(state: dict) -> str:
    name = state.get("merchant_name", "there")
    return f"Understood — I'll stop here. All the best with the business, and I'm around if that changes."


def _off_topic_redirect(state: dict) -> str:
    return ("That's outside what I can help with directly, sorry — I'm just here for your listing/growth side. "
            "Back to what we were on: want me to go ahead with the draft?")


def _action_mode_response(state: dict, merchant_message: str) -> str:
    hindi = _lang_pack(state, merchant_message)
    if hindi:
        return "Done — abhi shuru kar rahi hoon. Kuch minute mein draft ready hoga, main yahin bhej dungi review ke liye."
    return "Done — starting now. I'll have a draft ready in a few minutes and send it here for you to review."


def _pitch_mode_response(state: dict, merchant_message: str) -> str:
    hindi = _lang_pack(state, merchant_message)
    if hindi:
        return "Theek hai! Bataiye, kya aap isse aage badhana chahenge — main abhi shuru kar sakti hoon."
    return "Good to know — want me to go ahead and set that up, or is there something you'd want to see first?"
