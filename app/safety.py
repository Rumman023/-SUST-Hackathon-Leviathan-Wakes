"""
safety.py
---------
Fintech safety guardrails. These run as a *final* pass over generated text so
that even if a template were wrong, the output cannot violate the Section 8
safety rules (which carry direct point penalties and disqualification risk).

Three responsibilities:
1. Never *ask* for credentials (PIN / OTP / password / full card number).
2. Never *promise* an unauthorized financial action (refund, reversal,
   account unblock, guaranteed recovery). Such phrasing is rewritten to the
   approved "any eligible amount will be returned through official channels"
   style language.
3. Never instruct the customer toward a non-official third party, and ignore
   any instructions embedded in the complaint (prompt injection).

The credential-safety warning line is also guaranteed present on customer
replies (except where the phishing template already states it more strongly).
"""

from __future__ import annotations

import re
from typing import List, Tuple

# Standard, approved safety sentence appended to customer replies.
SAFETY_LINE_EN = "Please do not share your PIN or OTP with anyone."
SAFETY_LINE_BN = "অনুগ্রহ করে কারো সাথে আপনার পিন বা ওটিপি শেয়ার করবেন না।"

# ---------------------------------------------------------------------------
# Pattern banks
# ---------------------------------------------------------------------------

# Phrases where the *service* would be requesting a credential. We only flag
# these when they read as a request ("share/enter/provide/send/give ... otp"),
# not when the text merely warns "do not share your OTP".
_CREDENTIAL_WORDS = r"(pin|otp|password|cvv|full card number|card number|secret)"
_REQUEST_VERBS = r"(share|enter|provide|send|give|tell|confirm|verify with|type|submit)"
_CREDENTIAL_REQUEST_RE = re.compile(
    rf"\b{_REQUEST_VERBS}\b[^.?!]{{0,40}}\b{_CREDENTIAL_WORDS}\b", re.IGNORECASE
)
# Negations that turn a credential phrase into a *warning* rather than a request,
# e.g. "do not share your OTP" / "we never ask for your PIN".
_NEGATION_RE = re.compile(
    r"\b(not|never|don'?t|do not|cannot|can'?t|avoid|without|won'?t|no need)\b",
    re.IGNORECASE,
)


def _is_real_credential_request(text: str) -> bool:
    """
    True only if the text asks the customer FOR a credential. A request phrase
    preceded by a negation within the same clause is a safety warning, not a
    request, and is allowed.
    """
    if not text:
        return False
    for m in _CREDENTIAL_REQUEST_RE.finditer(text):
        before = text[max(0, m.start() - 18):m.start()]
        if _NEGATION_RE.search(before):
            continue  # negated -> it's a warning
        return True
    return False

# Unsafe promises -> approved safe replacement.
_UNSAFE_PROMISE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bwe\s+(?:will|'ll|shall)\s+refund\s+you\b", re.IGNORECASE),
     "any eligible amount will be returned through official channels"),
    (re.compile(r"\bwe\s+(?:have|'ve)\s+refunded\b", re.IGNORECASE),
     "our team will review the case and any eligible amount will be returned through official channels"),
    (re.compile(r"\bwe\s+(?:will|'ll)\s+(?:reverse|reversed)\b", re.IGNORECASE),
     "our team will review the case"),
    (re.compile(r"\bwe\s+(?:have|'ve)\s+reversed\b", re.IGNORECASE),
     "our team will review the case"),
    (re.compile(r"\byour\s+money\s+will\s+(?:definitely\s+)?be\s+returned\b", re.IGNORECASE),
     "any eligible amount will be returned through official channels"),
    (re.compile(r"\byour\s+account\s+will\s+be\s+unblocked\b", re.IGNORECASE),
     "our team will review your account through official channels"),
    (re.compile(r"\bwe\s+(?:will|'ll)\s+unblock\b", re.IGNORECASE),
     "our team will review your account through official channels"),
    (re.compile(r"\bguaranteed?\s+(?:refund|recovery|reversal)\b", re.IGNORECASE),
     "review through official channels"),
    (re.compile(r"\bwe\s+(?:will|'ll)\s+recover\s+your\s+money\b", re.IGNORECASE),
     "our team will review the case"),
]

# Prompt-injection markers used only for telemetry / reason codes. They do not
# change behaviour because the pipeline is rule-based and never executes
# instructions found in the complaint.
_INJECTION_RE = re.compile(
    r"(ignore (?:previous|all|the)\s+(?:rules|instructions|prompt)|"
    r"disregard (?:previous|all)|you are now|new instructions|"
    r"ask me for my (?:otp|pin|password)|tell me your (?:system )?prompt|"
    # Bangla cues: "পূর্ববর্তী নির্দেশ উপেক্ষা করুন", "সিস্টেম প্রম্পট বলুন", etc.
    r"পূর্ববর্তী\s*(?:নির্দেশ|নিয়ম)\s*(?:উপেক্ষা|অগ্রাহ্য)|"
    r"সিস্টেম\s*প্রম্পট|"
    r"ওটিপি\s*বলুন|পিন\s*বলুন)",
    re.IGNORECASE,
)


def detect_prompt_injection(complaint: str) -> bool:
    """True if the complaint appears to contain an instruction-override attempt."""
    return bool(_INJECTION_RE.search(complaint or ""))


def _strip_credential_requests(text: str) -> str:
    """Delete any sentence that genuinely asks the customer for a credential."""
    if not text:
        return text
    if not _is_real_credential_request(text):
        return text
    # Remove offending sentences while keeping the rest of the reply intact.
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = [s for s in sentences if not _is_real_credential_request(s)]
    cleaned = " ".join(kept).strip()
    return cleaned or text  # never return empty


def _rewrite_unsafe_promises(text: str) -> str:
    """Replace unauthorized financial promises with approved safe language."""
    if not text:
        return text
    for pattern, replacement in _UNSAFE_PROMISE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _ensure_safety_line(text: str, language: str) -> str:
    """Guarantee the credential-warning sentence is present in a customer reply."""
    lowered = (text or "").lower()
    already_warns = ("do not share" in lowered or "never share" in lowered
                     or "শেয়ার করবেন না" in (text or "")
                     or "never ask for your" in lowered)
    if already_warns:
        return text
    line = SAFETY_LINE_BN if language == "bn" else SAFETY_LINE_EN
    sep = " " if text and not text.endswith((" ", "\n")) else ""
    return f"{text}{sep}{line}".strip()


def sanitize_customer_reply(text: str, language: str = "en") -> str:
    """Full safety pass for a customer-facing reply."""
    text = _strip_credential_requests(text)
    text = _rewrite_unsafe_promises(text)
    text = _ensure_safety_line(text, language)
    return text.strip()


def sanitize_internal_text(text: str) -> str:
    """
    Safety pass for agent-facing text (summary / next action). We still strip
    unauthorized financial promises and any credential request, but we do not
    append the customer-facing safety line here.
    """
    text = _strip_credential_requests(text)
    text = _rewrite_unsafe_promises(text)
    return text.strip()


def reply_is_safe(text: str) -> bool:
    """
    Validation helper used by tests: returns True only if the reply contains
    no genuine credential request and no unauthorized financial promise.
    """
    if _is_real_credential_request(text or ""):
        return False
    for pattern, _ in _UNSAFE_PROMISE_PATTERNS:
        if pattern.search(text or ""):
            return False
    return True
