"""
contexts.py — safe accessors over the raw dicts loaded from the dataset JSON.

We deliberately do NOT build strict dataclasses that raise on missing fields.
Real pushes (per challenge-testing-brief.md §4 Phase 3) can be partial,
generated (non-seed) merchants/triggers are sparser than the seeds, and a
composer that crashes on a missing key is worse than one that degrades
gracefully. Every getter here returns a safe default instead of KeyError.
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


# ---------------------------------------------------------------------------
# generic safe getters
# ---------------------------------------------------------------------------

def g(d: Optional[dict], *path, default=None):
    """Nested safe-get: g(merchant, 'identity', 'name', default='there')."""
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


def is_placeholder_payload(trigger: dict) -> bool:
    """Generated (non-seed) triggers carry payload={'placeholder': True, ...}
    with no real facts. Composer must not cite numbers from these."""
    return bool(g(trigger, "payload", "placeholder", default=False))


# ---------------------------------------------------------------------------
# category helpers
# ---------------------------------------------------------------------------

def digest_by_id(category: dict, item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    for item in g(category, "digest", default=[]) or []:
        if item.get("id") == item_id:
            return item
    return None


def digest_by_kind(category: dict, kind: str) -> list[dict]:
    return [d for d in (g(category, "digest", default=[]) or []) if d.get("kind") == kind]


def freshest_digest_for_trigger(category: dict, trigger: dict, *kinds: str) -> Optional[dict]:
    """Resolve a trigger to its supplied digest item, then to a fresh relevant item.

    Phase 3 of the official harness replaces category contexts with newly
    injected digest entries.  Falling back to the last matching entry lets a
    known trigger family benefit from that newer context without assuming a
    seed-only identifier or inventing a citation.
    """
    payload = g(trigger, "payload", default={}) or {}
    for key in ("top_item_id", "digest_item_id", "item_id", "source_item_id"):
        item = digest_by_id(category, payload.get(key))
        if item:
            return item
    wanted = {kind.lower() for kind in kinds if kind}
    matches = [
        item for item in (g(category, "digest", default=[]) or [])
        if str(item.get("kind", "")).lower() in wanted
    ]
    return matches[-1] if matches else None


def seasonal_beat_for_month(category: dict, month_abbr: str) -> Optional[dict]:
    """month_abbr like 'Apr'. Matches against 'month_range' strings like 'Apr-Jun'."""
    for beat in g(category, "seasonal_beats", default=[]) or []:
        rng = beat.get("month_range", "")
        if month_abbr in rng:
            return beat
    return None


def taboo_words(category: dict) -> list[str]:
    return [w.lower() for w in g(category, "voice", "vocab_taboo", default=[]) or []]


def violates_taboo(text: str, category: dict) -> list[str]:
    low = text.lower()
    return [w for w in taboo_words(category) if w in low]


def uses_code_mix(category: dict) -> bool:
    return "hindi" in (g(category, "voice", "code_mix", default="") or "").lower()


# ---------------------------------------------------------------------------
# merchant helpers
# ---------------------------------------------------------------------------

def merchant_display_name(merchant: dict) -> str:
    return g(merchant, "identity", "name", default="there")


def owner_salutation(merchant: dict, category: dict) -> str:
    """Pick a salutation consistent with category voice + actual owner name."""
    first = g(merchant, "identity", "owner_first_name")
    examples = g(category, "voice", "salutation_examples", default=[]) or []
    if first:
        # dentists use "Dr. {first_name}" style if present in examples
        for ex in examples:
            if "{first_name}" in ex:
                return ex.replace("{first_name}", first)
        return first
    return merchant_display_name(merchant)


def merchant_speaks_hindi(merchant: dict) -> bool:
    return "hi" in (g(merchant, "identity", "languages", default=[]) or [])


def active_offers(merchant: dict) -> list[dict]:
    return [o for o in (g(merchant, "offers", default=[]) or []) if o.get("status") == "active"]


def has_signal(merchant: dict, prefix: str) -> Optional[str]:
    """merchant.signals is a list of free-form strings like 'stale_posts:22d'.
    Returns the first signal string starting with `prefix`, else None."""
    for s in g(merchant, "signals", default=[]) or []:
        if s.startswith(prefix):
            return s
    return None


def signal_number(signal_str: Optional[str]) -> Optional[str]:
    """'stale_posts:22d' -> '22d'. 'dormant_with_vera_38d' -> None (no colon) —
    caller should use regex for underscore-joined variants if needed."""
    if not signal_str or ":" not in signal_str:
        return None
    return signal_str.split(":", 1)[1]


def review_theme(merchant: dict, theme_substr: Optional[str] = None) -> Optional[dict]:
    themes = g(merchant, "review_themes", default=[]) or []
    if not themes:
        return None
    if theme_substr:
        for t in themes:
            if theme_substr in t.get("theme", ""):
                return t
    return themes[0]


def last_conversation_turn(merchant: dict) -> Optional[dict]:
    hist = g(merchant, "conversation_history", default=[]) or []
    return hist[-1] if hist else None


def merchant_engaged_recently(merchant: dict) -> bool:
    last = last_conversation_turn(merchant)
    if not last:
        return False
    return last.get("from") == "merchant" or "intent" in (last.get("engagement") or "")


# ---------------------------------------------------------------------------
# customer helpers
# ---------------------------------------------------------------------------

def customer_first_name(customer: dict) -> str:
    name = g(customer, "identity", "name", default="")
    # strip "(parent: X)" / "(walk-in, no profile)" annotations for the greeting
    if "(" in name:
        name = name.split("(")[0].strip()
    return name or "there"


def customer_wants_code_mix(customer: dict) -> bool:
    pref = (g(customer, "identity", "language_pref", default="") or "").lower()
    return "mix" in pref or pref in ("hi", "hindi")


def customer_pure_hindi(customer: dict) -> bool:
    pref = (g(customer, "identity", "language_pref", default="") or "").lower()
    return pref == "hi"


def customer_has_consent(customer: Optional[dict], required_scope: Optional[str | Iterable[str]]) -> bool:
    """Return whether a customer has consent for a trigger's outreach purpose.

    Customer triggers are not sufficient proof of consent: the judge can inject
    new customer contexts specifically to verify that outreach respects the
    consent scope supplied with that customer.
    """
    if not customer or not required_scope:
        return False
    scopes = g(customer, "consent", "scope", default=[]) or []
    required = {required_scope} if isinstance(required_scope, str) else set(required_scope)
    return bool(required & set(scopes))


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

def fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "?"
    return f"{'+' if x >= 0 else ''}{round(x * 100)}%"


def fmt_money(x) -> str:
    try:
        return f"₹{int(x):,}"
    except (TypeError, ValueError):
        return str(x)


def parse_iso_date(s: Optional[str]) -> Optional[datetime]:
    """Parses both bare dates ('2026-05-12') and full ISO timestamps
    ('2026-05-12T18:00:00+05:30' / '...Z'). Always returns a tz-aware
    datetime (naive dates are assumed UTC) so subtraction never raises."""
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        d = datetime.fromisoformat(s2)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except ValueError:
        return None


def days_between(iso_a: Optional[str], iso_b: Optional[str]) -> Optional[int]:
    a, b = parse_iso_date(iso_a), parse_iso_date(iso_b)
    if not a or not b:
        return None
    return abs((b - a).days)


def month_abbr(iso_s: Optional[str]) -> Optional[str]:
    d = parse_iso_date(iso_s)
    return d.strftime("%b") if d else None
