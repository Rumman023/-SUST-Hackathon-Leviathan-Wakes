"""
utils.py
--------
Deterministic text-processing helpers for the QueueStorm Investigator.

Everything here is pure (no I/O, no network) so it is trivially testable and
fast. The functions handle English, Bangla, and Banglish ("mixed") text:

- Bangla numerals (০-৯) are folded to ASCII digits.
- Money amounts are extracted while deliberately ignoring phone numbers,
  clock times ("2pm", "14:08") and ID-like tokens ("TXN-9101").
- Bangladeshi phone numbers are extracted and normalised to their last 10
  digits so "01712345678" and "+8801712345678" compare equal.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional

# ---------------------------------------------------------------------------
# Bangla numeral folding
# ---------------------------------------------------------------------------
_BANGLA_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


def fold_bangla_digits(text: str) -> str:
    """Replace Bangla numerals with their ASCII equivalents."""
    return (text or "").translate(_BANGLA_DIGITS)


def contains_bangla(text: str) -> bool:
    """True if the string contains any Bangla (Bengali) script character."""
    if not text:
        return False
    return any("\u0980" <= ch <= "\u09FF" for ch in text)


def detect_language(complaint: str, declared: Optional[str]) -> str:
    """
    Resolve the reply language.

    A declared language ('en' | 'bn' | 'mixed') wins when present and valid.
    Otherwise we sniff the complaint: Bangla script present -> 'bn', else 'en'.
    """
    if declared in {"en", "bn", "mixed"}:
        return declared
    return "bn" if contains_bangla(complaint) else "en"


def normalize_text(text: str) -> str:
    """
    Lowercased, digit-folded, whitespace-collapsed copy of the text used for
    keyword matching. The original (with Bangla script) is preserved by the
    caller; this is only for case-insensitive English/Banglish matching.
    """
    if not text:
        return ""
    t = fold_bangla_digits(text).lower()
    t = re.sub(r"\s+", " ", t)
    return t.strip()


# ---------------------------------------------------------------------------
# Amount extraction
# ---------------------------------------------------------------------------
_PHONE_INTL_RE = re.compile(r"\+?8801\d{9}")
_PHONE_LOCAL_RE = re.compile(r"\b01\d{9}\b")
_TIME_AMPM_RE = re.compile(r"\b\d{1,2}\s*[ap]\.?m\.?\b", re.IGNORECASE)
_TIME_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
_ID_TOKEN_RE = re.compile(r"\b[a-z]{2,}-?\d+\b", re.IGNORECASE)  # TXN-9101, AGENT-512
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Words that strongly indicate a money value follows/precedes a number.
_MONEY_WORDS = ("taka", "tk", "tk.", "bdt", "t@ka", "paisa", "tka")


def extract_amounts(raw_text: str) -> List[float]:
    """
    Return plausible BDT amounts mentioned in the complaint.

    Phone numbers, clock times and ID tokens are stripped first so they are
    never misread as money. Amounts are de-duplicated while preserving order.
    """
    text = fold_bangla_digits(raw_text or "")
    # Remove things that look like numbers but are not money.
    text = _PHONE_INTL_RE.sub(" ", text)
    text = _PHONE_LOCAL_RE.sub(" ", text)
    text = _ID_TOKEN_RE.sub(" ", text)
    text = _TIME_AMPM_RE.sub(" ", text)
    text = _TIME_CLOCK_RE.sub(" ", text)

    amounts: List[float] = []
    seen = set()
    for match in _NUMBER_RE.finditer(text):
        token = match.group(0).replace(",", "")
        try:
            value = float(token)
        except ValueError:
            continue
        # Ignore absurd / non-money values and bare years.
        if value < 1 or value > 10_000_000:
            continue
        key = round(value, 2)
        if key not in seen:
            seen.add(key)
            amounts.append(value)
    return amounts


# ---------------------------------------------------------------------------
# Phone / counterparty extraction
# ---------------------------------------------------------------------------
def normalize_phone(value: Optional[str]) -> Optional[str]:
    """Reduce a phone-like string to its last 10 significant digits."""
    if not value:
        return None
    digits = re.sub(r"\D", "", fold_bangla_digits(value))
    if len(digits) >= 10:
        return digits[-10:]
    return digits or None


def extract_phones(raw_text: str) -> List[str]:
    """Extract Bangladeshi phone numbers, normalised to last 10 digits."""
    text = fold_bangla_digits(raw_text or "")
    found = _PHONE_INTL_RE.findall(text) + _PHONE_LOCAL_RE.findall(text)
    out: List[str] = []
    seen = set()
    for f in found:
        n = normalize_phone(f)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------
def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, tolerating a trailing 'Z'. None on failure."""
    if not value or not isinstance(value, str):
        return None
    try:
        cleaned = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def amounts_match(a: Optional[float], b: Optional[float], tol: float = 0.5) -> bool:
    """True if two amounts are equal within a small absolute tolerance."""
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol
