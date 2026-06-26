"""
reasoning.py
------------
The deterministic investigator engine. Given a parsed ticket, it:

  1. Detects case_type from the complaint (keyword cascade, EN + Bangla).
  2. Matches the complaint against transaction_history (the "investigator
     twist"): picks the relevant transaction or decides none/ambiguous.
  3. Decides evidence_verdict (consistent / inconsistent / insufficient_data)
     by comparing the claim to the matched transaction's type & status and to
     established-recipient / duplicate patterns.
  4. Routes (department), grades (severity), and decides human_review_required.
  5. Writes agent_summary, recommended_next_action and a safe customer_reply.

Why rule-based: the rubric scores schema correctness, evidence reasoning and
safety deterministically. A rule engine gives reproducible, explainable, fast
(<5 ms) answers with zero external dependencies and no possibility of an LLM
inventing an unsafe promise or an out-of-vocabulary enum. An optional LLM hook
(app/llm.py) can *polish* wording but never makes the structured decisions.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from .models import AnalyzeRequest, TransactionHistoryEntry
from .safety import (
    detect_prompt_injection,
    sanitize_customer_reply,
    sanitize_internal_text,
)
from .utils import (
    amounts_match,
    detect_language,
    extract_amounts,
    extract_phones,
    normalize_phone,
    normalize_text,
    parse_timestamp,
)

# Suspicious contact-channel cues: a complaint that asks the agent to call
# back a non-official number, or to email an address the customer supplied,
# is a classic fraud pattern. We surface it as a reason_code only (the
# routing/severity are already handled by the phishing cascade when present).
_SUSPICIOUS_CHANNEL_CUES = [
    "call me at", "call back at", "email me at", "send to", "mail to",
    "contact me on", "reach me at", "এই নম্বরে কল", "এই নম্বরে যোগাযোগ",
]

HIGH_VALUE_THRESHOLD = 5000.0  # BDT; tunable, matches problem-statement guidance
EXTRA_HIGH_VALUE_THRESHOLD = 50000.0  # BDT; bumps severity a tier for huge claims

# ---------------------------------------------------------------------------
# Keyword banks (English + Banglish + Bangla script)
# ---------------------------------------------------------------------------
_KW = {
    "phishing": [
        "otp", "one time password", "pin code", "password",
        "scam", "fraud", "phishing", "suspicious call", "suspicious sms",
        "fake call", "asking for my otp", "asked for my otp", "asking for otp",
        "account will be blocked", "account block", "blocked if",
        "they called", "someone called", "claiming to be", "pretend",
        "prize", "lottery", "won", "verify your account",
        "প্রতারণা", "ওটিপি চাইছে", "ওটিপি চেয়েছে", "সন্দেহজনক", "ব্লক",
    ],
    "duplicate": [
        "twice", "two times", "double", "duplicate", "deducted twice",
        "charged twice", "paid twice", "two payments", "second time",
        "duibar", "dui bar", "du bar", "duible",
        "দুইবার", "দুবার", "দুই বার", "ডবল",
    ],
    "agent_cash_in": [
        "cash in", "cash-in", "cashin", "deposited", "deposit",
        "agent", "through agent", "agent er kache", "agent ke",
        "balance not updated", "balance not reflected", "not reflected",
        "ক্যাশ ইন", "ক্যাশইন", "এজেন্ট", "জমা",
    ],
    "settlement": [
        "settlement", "settle", "not settled", "settled to my account",
        "merchant settlement", "payout", "disbursement",
        "সেটেলমেন্ট", "নিষ্পত্তি",
    ],
    "payment_failed": [
        "failed", "failure", "transaction failed", "payment failed",
        "recharge failed", "showed failed", "did not go through",
        "didn't go through", "balance was deducted", "balance deducted",
        "money deducted", "deducted but", "kete gese", "kete geche",
        "taka kete", "taka keteche", "kete niyeche",
        "ফেইল", "ব্যর্থ", "কেটে গেছে", "কেটে নিয়েছে", "টাকা কেটেছে",
    ],
    "wrong_transfer": [
        "wrong number", "wrong person", "wrong recipient", "wrong account",
        "mistakenly sent", "sent to wrong", "sent by mistake", "typed it wrong",
        "reverse it", "reverse the transfer", "didn't get it", "did not get it",
        "didn't receive", "did not receive", "hasn't received", "has not received",
        "didn't reach", "bhul number", "bhul number e", "bhul manush",
        "vul number", "wrong e", "ভুল নম্বর", "ভুল মানুষ", "ভুল নাম্বার",
        "পায়নি", "পাইনি", "পৌঁছায়নি",
    ],
    "refund": [
        "refund", "money back", "return my", "want my money back",
        "changed my mind", "change my mind", "don't want", "do not want",
        "product issue", "defective", "cancel", "ferot", "ferot chai",
        "রিফান্ড", "ফেরত", "টাকা ফেরত",
    ],
}


def _has_any(text: str, keywords: List[str]) -> bool:
    return any(kw in text for kw in keywords)


def _count_hits(text: str, keywords: List[str]) -> int:
    return sum(1 for kw in keywords if kw in text)


# ---------------------------------------------------------------------------
# 1. Case-type detection
# ---------------------------------------------------------------------------
def detect_case_type(req: AnalyzeRequest, norm: str, raw: str) -> str:
    """
    Ordered cascade. Order encodes precedence learned from the taxonomy:
    phishing (safety) > duplicate > agent cash-in > settlement >
    payment_failed > wrong_transfer > refund > other.
    """
    text = norm + " " + raw  # raw keeps Bangla script for matching

    phishing_signals = _count_hits(text, _KW["phishing"])
    # A bare mention of "password"/"pin" inside an injection attempt should not
    # by itself flip the whole case to phishing; require a genuine report
    # signal (someone contacting them / scam framing) OR multiple hits.
    report_framing = _has_any(
        text,
        ["called", "call", "sms", "message", "claiming", "scam", "fraud",
         "blocked", "block", "asked", "asking", "ফোন", "কল", "এসএমএস", "প্রতারণা"],
    )
    if phishing_signals >= 1 and report_framing:
        return "phishing_or_social_engineering"
    if phishing_signals >= 2:
        return "phishing_or_social_engineering"

    if _has_any(text, _KW["duplicate"]):
        return "duplicate_payment"

    is_merchant = (req.user_type == "merchant") or (req.channel == "merchant_portal")
    if _has_any(text, _KW["settlement"]) and (is_merchant or "settlement" in text):
        return "merchant_settlement_delay"

    # Agent cash-in needs both a cash-in/deposit cue and an agent cue, OR an
    # explicit "cash in" phrase, to avoid grabbing generic "agent" mentions.
    has_cashin_word = _has_any(text, ["cash in", "cash-in", "cashin", "deposit",
                                      "deposited", "ক্যাশ ইন", "ক্যাশইন", "জমা"])
    has_agent_word = _has_any(text, ["agent", "এজেন্ট"])
    if has_cashin_word and (has_agent_word or "cash in" in text or "ক্যাশ ইন" in text):
        return "agent_cash_in_issue"

    if _has_any(text, _KW["payment_failed"]):
        return "payment_failed"

    if _has_any(text, _KW["wrong_transfer"]):
        return "wrong_transfer"

    if _has_any(text, _KW["refund"]):
        return "refund_request"

    return "other"


# ---------------------------------------------------------------------------
# Transaction helpers
# ---------------------------------------------------------------------------
def _txns(req: AnalyzeRequest) -> List[TransactionHistoryEntry]:
    return [t for t in (req.transaction_history or []) if t is not None]


def _by_type(txns: List[TransactionHistoryEntry], types: Tuple[str, ...]):
    return [t for t in txns if (t.type or "").lower() in types]


def _by_amount(txns: List[TransactionHistoryEntry], amounts: List[float]):
    if not amounts:
        return list(txns)
    return [t for t in txns if any(amounts_match(t.amount, a) for a in amounts)]


def _by_phone(txns: List[TransactionHistoryEntry], phones: List[str]):
    if not phones:
        return []
    out = []
    for t in txns:
        cp = normalize_phone(t.counterparty) if t.counterparty else None
        if cp and cp in phones:
            out.append(t)
    return out


def _most_recent(txns: List[TransactionHistoryEntry]):
    if not txns:
        return None

    def key(t):
        dt = parse_timestamp(t.timestamp)
        return dt.timestamp() if dt else float("-inf")

    return max(txns, key=key)


def _distinct_counterparties(txns: List[TransactionHistoryEntry]) -> int:
    """Count unique counterparties with phone numbers normalised, so
    01712345678 and +8801712345678 collapse to the same recipient."""
    seen = set()
    for t in txns:
        cp = (t.counterparty or "").strip()
        if not cp:
            continue
        key = normalize_phone(cp) if cp else cp
        seen.add(key)
    return len(seen)


# ---------------------------------------------------------------------------
# 2 + 3. Per-case matching & verdict
# ---------------------------------------------------------------------------
def _investigate(
    case_type: str,
    req: AnalyzeRequest,
    amounts: List[float],
    phones: List[str],
) -> Tuple[Optional[TransactionHistoryEntry], str, List[str]]:
    """
    Returns (relevant_transaction, evidence_verdict, reason_codes).
    Dispatches to case-specific logic.
    """
    txns = _txns(req)
    if not txns:
        # Safety-only / history-less cases.
        if case_type == "phishing_or_social_engineering":
            return None, "insufficient_data", ["no_transaction_history"]
        return None, "insufficient_data", ["no_transaction_history"]

    if case_type == "wrong_transfer":
        return _investigate_wrong_transfer(txns, amounts, phones)
    if case_type == "payment_failed":
        return _investigate_payment_failed(txns, amounts)
    if case_type == "refund_request":
        return _investigate_refund(txns, amounts)
    if case_type == "duplicate_payment":
        return _investigate_duplicate(txns, amounts)
    if case_type == "merchant_settlement_delay":
        return _investigate_settlement(txns, amounts)
    if case_type == "agent_cash_in_issue":
        return _investigate_cash_in(txns, amounts)
    if case_type == "phishing_or_social_engineering":
        return None, "insufficient_data", ["safety_report"]
    # other
    return None, "insufficient_data", ["vague_complaint"]


def _investigate_wrong_transfer(txns, amounts, phones):
    transfers = _by_type(txns, ("transfer",))
    pool = transfers or txns

    # Prefer a phone-narrowed candidate when the complaint names a number.
    phone_hits = _by_phone(pool, phones)
    amount_hits = _by_amount(pool, amounts)

    if phone_hits and len(phone_hits) == 1:
        candidate = phone_hits[0]
    elif phone_hits and amounts:
        narrowed = _by_amount(phone_hits, amounts)
        candidate = narrowed[0] if len(narrowed) == 1 else (_most_recent(phone_hits))
    else:
        candidates = amount_hits if amounts else pool
        if not candidates:
            return None, "insufficient_data", ["no_match"]
        if len(candidates) == 1:
            candidate = candidates[0]
        else:
            # Ambiguous if they point to different recipients and we cannot
            # disambiguate -> do NOT guess (SAMPLE-08 behaviour).
            if _distinct_counterparties(candidates) > 1:
                return None, "insufficient_data", ["ambiguous_match", "needs_clarification"]
            candidate = _most_recent(candidates)

    # Established-recipient check: repeated transfers to the same counterparty
    # contradict a "wrong recipient" claim (SAMPLE-02 behaviour).
    # Phone numbers are normalised to last-10-digits so 01712345678 and
    # +8801712345678 compare equal.
    cp = (candidate.counterparty or "").strip()
    cp_norm = normalize_phone(cp) if cp else None

    def _same_recipient(t):
        other = (t.counterparty or "").strip()
        if not other:
            return False
        if cp_norm:
            return normalize_phone(other) == cp_norm
        return other == cp

    same_cp = [t for t in transfers if _same_recipient(t)]
    if len(same_cp) >= 2:
        return candidate, "inconsistent", [
            "wrong_transfer_claim", "established_recipient_pattern", "evidence_inconsistent"
        ]
    return candidate, "consistent", ["wrong_transfer", "transaction_match"]


def _investigate_payment_failed(txns, amounts):
    payments = _by_type(txns, ("payment",))
    pool = payments or txns
    amount_hits = _by_amount(pool, amounts) if amounts else pool

    # A failed payment is the textbook "balance deducted, item not delivered"
    # case -> consistent.
    failed = [t for t in amount_hits if (t.status or "").lower() == "failed"]
    if failed:
        return _most_recent(failed), "consistent", ["payment_failed", "potential_balance_deduction"]

    # A pending payment also means the customer's money has not yet resolved,
    # so a "failed but deducted" complaint is supported.
    pending = [t for t in amount_hits if (t.status or "").lower() == "pending"]
    if pending:
        return _most_recent(pending), "consistent", ["payment_failed", "pending_payment", "potential_balance_deduction"]

    if amount_hits:
        # Claimed failure but the matching transaction completed -> contradiction.
        completed = [t for t in amount_hits if (t.status or "").lower() == "completed"]
        if completed:
            return _most_recent(completed), "inconsistent", ["payment_failed_claim", "status_completed", "evidence_inconsistent"]
        return _most_recent(amount_hits), "consistent", ["payment_failed", "transaction_match"]

    return None, "insufficient_data", ["no_match"]


def _investigate_refund(txns, amounts):
    refunds = _by_type(txns, ("refund",))
    payments = _by_type(txns, ("payment",))

    # If a refund already exists for the amount and is completed/reversed, a
    # "refund not received" claim is contradicted by the data.
    refund_hits = _by_amount(refunds, amounts) if amounts else refunds
    settled_refund = [t for t in refund_hits if (t.status or "").lower() in ("completed", "reversed")]
    if settled_refund:
        return _most_recent(settled_refund), "inconsistent", ["refund_already_processed", "evidence_inconsistent"]

    # Otherwise tie the refund request to the original completed payment.
    pay_hits = _by_amount(payments, amounts) if amounts else payments
    completed_pay = [t for t in pay_hits if (t.status or "").lower() == "completed"]
    if completed_pay:
        return _most_recent(completed_pay), "consistent", ["refund_request", "merchant_policy_dependent"]
    if pay_hits:
        return _most_recent(pay_hits), "consistent", ["refund_request", "transaction_match"]

    return None, "insufficient_data", ["no_match"]


def _investigate_duplicate(txns, amounts):
    payments = _by_type(txns, ("payment",))
    pool = payments or txns

    # Group by (amount, counterparty); a group of >=2 is a duplicate signal.
    groups: Dict[Tuple, List] = defaultdict(list)
    for t in pool:
        groups[(round(t.amount, 2) if t.amount is not None else None,
                (t.counterparty or "").strip())].append(t)

    dup_groups = [g for g in groups.values() if len(g) >= 2]
    if amounts:
        dup_groups = [g for g in dup_groups if any(amounts_match(g[0].amount, a) for a in amounts)] or dup_groups

    if dup_groups:
        # Pick the largest, most recent group; relevant txn = the later one.
        group = max(dup_groups, key=len)
        later = _most_recent(group)
        return later, "consistent", ["duplicate_payment", "biller_verification_required"]

    # Claim of duplicate but only one matching payment -> contradicted.
    amount_hits = _by_amount(pool, amounts) if amounts else pool
    if len(amount_hits) == 1:
        return amount_hits[0], "inconsistent", ["duplicate_claim", "single_transaction_only", "evidence_inconsistent"]
    if amount_hits:
        return _most_recent(amount_hits), "insufficient_data", ["duplicate_unclear"]
    return None, "insufficient_data", ["no_match"]


def _investigate_settlement(txns, amounts):
    settlements = _by_type(txns, ("settlement",))
    pool = settlements or _by_type(txns, ("payment",)) or txns
    amount_hits = _by_amount(pool, amounts) if amounts else pool

    pending = [t for t in amount_hits if (t.status or "").lower() in ("pending", "failed")]
    if pending:
        return _most_recent(pending), "consistent", ["merchant_settlement", "delay", "pending"]

    completed = [t for t in amount_hits if (t.status or "").lower() in ("completed", "reversed")]
    if completed:
        return _most_recent(completed), "inconsistent", ["settlement_completed", "evidence_inconsistent"]

    if amount_hits:
        return _most_recent(amount_hits), "consistent", ["merchant_settlement", "transaction_match"]
    return None, "insufficient_data", ["no_match"]


def _investigate_cash_in(txns, amounts):
    cash_ins = _by_type(txns, ("cash_in",))
    pool = cash_ins or txns
    amount_hits = _by_amount(pool, amounts) if amounts else pool

    pending = [t for t in amount_hits if (t.status or "").lower() in ("pending", "failed")]
    if pending:
        return _most_recent(pending), "consistent", ["agent_cash_in", "pending_transaction", "agent_ops"]

    completed = [t for t in amount_hits if (t.status or "").lower() == "completed"]
    if completed:
        # Completed cash-in but customer says it didn't arrive -> data conflict.
        return _most_recent(completed), "inconsistent", ["agent_cash_in_claim", "status_completed", "evidence_inconsistent"]

    if amount_hits:
        return _most_recent(amount_hits), "consistent", ["agent_cash_in", "transaction_match"]
    return None, "insufficient_data", ["no_match"]


# ---------------------------------------------------------------------------
# 4. Routing / grading / escalation
# ---------------------------------------------------------------------------
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _bump_severity(sev: str, amount: Optional[float]) -> str:
    """Upgrade severity by one tier for very high-value claims."""
    if amount is not None and amount >= EXTRA_HIGH_VALUE_THRESHOLD:
        upgraded = {"low": "medium", "medium": "high", "high": "critical", "critical": "critical"}
        return upgraded[sev]
    return sev


def _severity_for(case_type: str, verdict: str, amount: Optional[float]) -> str:
    if case_type == "phishing_or_social_engineering":
        return "critical"
    if case_type == "wrong_transfer":
        return "high" if verdict == "consistent" else "medium"
    if case_type in ("payment_failed", "duplicate_payment", "agent_cash_in_issue"):
        return _bump_severity("high", amount)
    if case_type == "merchant_settlement_delay":
        return "medium"
    if case_type == "refund_request":
        if amount is not None and amount >= HIGH_VALUE_THRESHOLD:
            return "medium"
        return "low"
    sev = "low"  # other
    return _bump_severity(sev, amount)


def _department_for(case_type: str, severity: str) -> str:
    mapping = {
        "phishing_or_social_engineering": "fraud_risk",
        "wrong_transfer": "dispute_resolution",
        "payment_failed": "payments_ops",
        "duplicate_payment": "payments_ops",
        "merchant_settlement_delay": "merchant_operations",
        "agent_cash_in_issue": "agent_operations",
        "other": "customer_support",
    }
    if case_type == "refund_request":
        return "dispute_resolution" if severity in ("high", "critical") else "customer_support"
    return mapping.get(case_type, "customer_support")


def _human_review_for(case_type, severity, verdict, relevant_id, reason_codes=None) -> bool:
    if case_type == "phishing_or_social_engineering":
        return True
    if case_type == "wrong_transfer":
        # A dispute we can act on needs review; a "needs clarification" (no txn)
        # does not yet (SAMPLE-08).
        return relevant_id is not None
    if case_type in ("duplicate_payment", "agent_cash_in_issue"):
        return relevant_id is not None
    if case_type == "refund_request":
        return severity in ("high", "critical")
    if case_type in ("payment_failed", "merchant_settlement_delay"):
        return verdict == "inconsistent"
    # Vague "other" complaints with risk signals (contact-channel request,
    # suspicious third party) escalate; clean vague cases match the reference
    # (SAMPLE-06: human_review_required == false).
    if case_type == "other" and reason_codes:
        risky = {"suspicious_contact_channel_requested",
                 "suspicious_third_party_mentioned",
                 "needs_clarification"}
        return bool(set(reason_codes) & risky) or verdict == "inconsistent"
    return False


def _confidence_for(case_type, verdict, relevant_id, reason_codes) -> float:
    if case_type == "phishing_or_social_engineering":
        return 0.95
    if "ambiguous_match" in reason_codes:
        return 0.65
    if verdict == "consistent":
        if case_type == "duplicate_payment":
            return 0.93
        if case_type == "merchant_settlement_delay":
            return 0.92
        if case_type == "agent_cash_in_issue":
            return 0.88
        return 0.9
    if verdict == "inconsistent":
        return 0.75
    if relevant_id is None:
        return 0.6
    return 0.7


# ---------------------------------------------------------------------------
# 5. Text generation (agent_summary / next_action / customer_reply)
# ---------------------------------------------------------------------------
def _fmt_amount(a: Optional[float]) -> str:
    if a is None:
        return ""
    return str(int(a)) if float(a).is_integer() else f"{a:g}"


def _agent_summary(case_type, txn, verdict, amounts) -> str:
    tid = txn.transaction_id if txn else None
    amt = _fmt_amount(txn.amount) if txn and txn.amount is not None else (
        _fmt_amount(amounts[0]) if amounts else "")
    cp = txn.counterparty if txn and txn.counterparty else ""
    status = (txn.status or "") if txn else ""

    if case_type == "phishing_or_social_engineering":
        return ("Customer reports an unsolicited contact requesting credentials "
                "(possible social engineering). No credentials confirmed shared. "
                "Treat as a fraud-risk safety report.")
    if case_type == "other":
        return ("Customer reports a vague concern without specifying a transaction, "
                "amount, or issue. Insufficient detail to identify a relevant transaction.")

    if not txn:
        if case_type == "wrong_transfer":
            return (f"Customer reports a transfer{(' of ' + amt + ' BDT') if amt else ''} "
                    "was not received. Multiple plausible transactions exist and the "
                    "correct one cannot be determined without further input.")
        return (f"Customer reports a {case_type.replace('_', ' ')} issue"
                f"{(' for ' + amt + ' BDT') if amt else ''} but no matching transaction "
                "was found in the provided history.")

    base = {
        "wrong_transfer": f"Customer reports sending {amt} BDT via {tid} to {cp}, now believed to be the wrong recipient.",
        "payment_failed": f"Customer attempted a {amt} BDT payment ({tid}) reported as failed with a possible balance deduction.",
        "refund_request": f"Customer requests a refund of {amt} BDT for {tid} (merchant payment).",
        "duplicate_payment": f"Customer reports a duplicate payment of {amt} BDT to {cp}; {tid} appears to be the duplicate charge.",
        "merchant_settlement_delay": f"Merchant reports settlement {tid} of {amt} BDT delayed beyond the expected window (status: {status}).",
        "agent_cash_in_issue": f"Customer reports a {amt} BDT cash-in via {cp} ({tid}) not reflected in balance (status: {status}).",
    }.get(case_type, f"Customer complaint linked to transaction {tid} ({amt} BDT, status: {status}).")

    if verdict == "inconsistent":
        if case_type == "wrong_transfer":
            base += " History shows repeated prior transfers to the same counterparty, suggesting an established recipient."
        elif case_type == "duplicate_payment":
            base += " Only a single matching payment exists, which does not support the duplicate claim."
        elif case_type == "payment_failed":
            base += " The matching transaction status is completed, which conflicts with the failure claim."
        elif case_type == "agent_cash_in_issue":
            base += " The matching cash-in status is completed, which conflicts with the non-receipt claim."
        elif case_type == "merchant_settlement_delay":
            base += " The matching settlement is already completed."
    return base


def _next_action(case_type, txn, verdict, relevant_id) -> str:
    tid = txn.transaction_id if txn else None
    if case_type == "phishing_or_social_engineering":
        return ("Escalate to the fraud_risk team. Reassure the customer that the company "
                "never asks for PIN, OTP, or password, and log the reported contact for "
                "fraud-pattern analysis through official channels.")
    if case_type == "other":
        return ("Reply to the customer requesting specific details: transaction ID, amount, "
                "what went wrong, and approximate time, before any further action.")
    if not txn:
        if case_type == "wrong_transfer":
            return ("Ask the customer for the recipient's number to identify the correct "
                    "transaction. Do not initiate a dispute until the transaction is confirmed.")
        return ("Request more detail from the customer to locate the relevant transaction "
                "before taking any operational action.")

    if case_type == "wrong_transfer":
        if verdict == "inconsistent":
            return (f"Flag for human review. Verify with the customer whether {tid} was genuinely "
                    "a wrong transfer given the established pattern with this recipient.")
        return (f"Verify {tid} details with the customer and initiate the wrong-transfer dispute "
                "workflow per policy.")
    if case_type == "payment_failed":
        if verdict == "inconsistent":
            return (f"Review {tid}: the ledger shows a completed status. Confirm with the customer "
                    "and reconcile before any reversal.")
        return (f"Investigate the ledger status of {tid}. If balance was deducted on a failed "
                "payment, initiate the automatic reversal flow within standard SLA.")
    if case_type == "refund_request":
        return ("Inform the customer that refund eligibility depends on the merchant's own policy "
                "and guide them on contacting the merchant directly through official channels.")
    if case_type == "duplicate_payment":
        return (f"Verify the duplicate with payments_ops. If the biller confirms only one payment "
                f"was received, initiate the reversal of {tid} per policy.")
    if case_type == "merchant_settlement_delay":
        return (f"Route to merchant_operations to verify the settlement batch status for {tid}. "
                "If delayed, communicate a revised ETA to the merchant.")
    if case_type == "agent_cash_in_issue":
        return (f"Investigate the status of {tid} with agent operations. Confirm the settlement "
                "state and resolve within the standard cash-in SLA.")
    return f"Review transaction {tid} and proceed per the relevant operational workflow."


def _customer_reply(case_type, txn, verdict, relevant_id, language, amounts) -> str:
    tid = txn.transaction_id if txn else None
    amt = _fmt_amount(amounts[0]) if amounts else (
        _fmt_amount(txn.amount) if txn and txn.amount is not None else "")
    bn = (language == "bn")

    # ---- Bangla templates ------------------------------------------------
    if bn:
        if case_type == "phishing_or_social_engineering":
            return ("কোনো তথ্য শেয়ার করার আগে যোগাযোগ করার জন্য ধন্যবাদ। আমরা কখনো আপনার "
                    "পিন, ওটিপি বা পাসওয়ার্ড চাই না। কেউ নিজেকে আমাদের প্রতিনিধি দাবি করলেও "
                    "এগুলো কারো সাথে শেয়ার করবেন না। আমাদের ফ্রড টিমকে বিষয়টি জানানো হয়েছে।")
        if case_type == "other" or not txn:
            if case_type == "wrong_transfer":
                return ("আপনার বার্তার জন্য ধন্যবাদ। ঐ সময়ে একাধিক লেনদেন পাওয়া গেছে। সঠিক "
                        "লেনদেনটি শনাক্ত করতে অনুগ্রহ করে প্রাপকের নম্বরটি জানান। অনুগ্রহ করে "
                        "কারো সাথে আপনার পিন বা ওটিপি শেয়ার করবেন না।")
            return ("আপনার বার্তার জন্য ধন্যবাদ। দ্রুত সহায়তার জন্য অনুগ্রহ করে লেনদেন আইডি, "
                    "সংশ্লিষ্ট পরিমাণ এবং কী সমস্যা হয়েছে তা জানান। অনুগ্রহ করে কারো সাথে "
                    "আপনার পিন বা ওটিপি শেয়ার করবেন না।")
        ref = f" {tid}" if tid else ""
        if case_type == "merchant_settlement_delay":
            return (f"আপনার সেটেলমেন্ট{ref} এর বিষয়ে আমরা অবগত হয়েছি। আমাদের মার্চেন্ট "
                    "অপারেশন্স দল ব্যাচ স্ট্যাটাস যাচাই করে অফিসিয়াল চ্যানেলে আপনাকে জানাবে।")
        if case_type == "refund_request":
            return ("আপনার অনুরোধের জন্য ধন্যবাদ। সম্পন্ন হওয়া মার্চেন্ট পেমেন্টের রিফান্ড "
                    "মার্চেন্টের নিজস্ব নীতির উপর নির্ভর করে। অনুগ্রহ করে সরাসরি মার্চেন্টের সাথে "
                    "যোগাযোগ করুন। অনুগ্রহ করে কারো সাথে আপনার পিন বা ওটিপি শেয়ার করবেন না।")
        team = {
            "wrong_transfer": "ডিসপিউট",
            "payment_failed": "পেমেন্ট",
            "duplicate_payment": "পেমেন্ট",
            "agent_cash_in_issue": "এজেন্ট অপারেশন্স",
        }.get(case_type, "সাপোর্ট")
        money_line = ("প্রযোজ্য কোনো অর্থ অফিসিয়াল চ্যানেলের মাধ্যমে ফেরত দেওয়া হবে। "
                      if case_type in ("payment_failed", "duplicate_payment") else "")
        return (f"আপনার লেনদেন{ref} এর বিষয়ে আমরা অবগত হয়েছি। আমাদের {team} দল বিষয়টি "
                f"যাচাই করবে। {money_line}অফিসিয়াল চ্যানেলে আপনার সাথে যোগাযোগ করা হবে। "
                "অনুগ্রহ করে কারো সাথে আপনার পিন বা ওটিপি শেয়ার করবেন না।")

    # ---- English templates ----------------------------------------------
    if case_type == "phishing_or_social_engineering":
        return ("Thank you for reaching out before sharing any information. We never ask for "
                "your PIN, OTP, or password under any circumstances. Please do not share these "
                "with anyone, even if they claim to be from us. Our fraud team has been notified "
                "of this incident.")
    if case_type == "other" or not txn:
        if case_type == "wrong_transfer":
            amt_phrase = f" of {amt} BDT" if amt else ""
            return ("Thank you for reaching out. We see multiple transactions" + amt_phrase +
                    " around that time. Could you share the recipient's number so we can identify "
                    "the right transaction? Please do not share your PIN or OTP with anyone.")
        return ("Thank you for reaching out. To help you faster, please share the transaction ID, "
                "the amount involved, and a short description of what went wrong. Please do not "
                "share your PIN or OTP with anyone.")

    ref = f" {tid}" if tid else ""
    if case_type == "refund_request":
        return ("Thank you for reaching out. Refunds for completed merchant payments depend on the "
                "merchant's own policy. We recommend contacting the merchant directly. If you need "
                "help reaching them, please reply and we will guide you through official support "
                "channels. Please do not share your PIN or OTP with anyone.")
    if case_type == "merchant_settlement_delay":
        return (f"We have noted your concern about settlement{ref}. Our merchant operations team "
                "will check the batch status and update you on the expected settlement time through "
                "official channels.")
    if case_type == "payment_failed":
        return (f"We have noted that transaction{ref} may have caused an unexpected balance "
                "deduction. Our payments team will review the case and any eligible amount will be "
                "returned through official channels. Please do not share your PIN or OTP with anyone.")
    if case_type == "duplicate_payment":
        return (f"We have noted the possible duplicate payment for transaction{ref}. Our payments "
                "team will verify with the biller and any eligible amount will be returned through "
                "official channels. Please do not share your PIN or OTP with anyone.")
    if case_type == "agent_cash_in_issue":
        return (f"We have noted your concern about transaction{ref}. Our agent operations team will "
                "verify the cash-in status and update you through official channels. Please do not "
                "share your PIN or OTP with anyone.")
    # wrong_transfer (consistent or inconsistent) -> safe dispute language
    return (f"We have noted your concern about transaction{ref}. Please do not share your PIN or "
            "OTP with anyone. Our dispute team will review the case and contact you through "
            "official support channels.")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def analyze(req: AnalyzeRequest) -> dict:
    """
    Run the full investigation and return a dict that conforms exactly to the
    response schema. Pure and deterministic.
    """
    raw = req.complaint or ""
    norm = normalize_text(raw)
    language = detect_language(raw, req.language)

    amounts = extract_amounts(raw)
    phones = extract_phones(raw)

    case_type = detect_case_type(req, norm, raw)
    txn, verdict, reason_codes = _investigate(case_type, req, amounts, phones)

    relevant_id = txn.transaction_id if txn else None
    amount_for_grade = (txn.amount if txn and txn.amount is not None
                        else (amounts[0] if amounts else None))

    severity = _severity_for(case_type, verdict, amount_for_grade)
    department = _department_for(case_type, severity)
    confidence = _confidence_for(case_type, verdict, relevant_id, reason_codes)

    agent_summary = _agent_summary(case_type, txn, verdict, amounts)
    next_action = _next_action(case_type, txn, verdict, relevant_id)
    customer_reply = _customer_reply(case_type, txn, verdict, relevant_id, language, amounts)

    # Defence-in-depth safety pass (also neutralises prompt injection effects).
    if detect_prompt_injection(raw):
        reason_codes = list(reason_codes) + ["prompt_injection_ignored"]
    # Flag suspicious contact-channel requests (asking the agent to call back
    # an external number / external email) as a reason_code for auditability.
    if any(cue in raw.lower() for cue in _SUSPICIOUS_CHANNEL_CUES):
        reason_codes = list(reason_codes) + ["suspicious_contact_channel_requested"]

    # Recompute human-review AFTER audit/safety flags are appended so risky
    # vague complaints (e.g. ask-to-call-back) are correctly escalated.
    human_review = _human_review_for(
        case_type, severity, verdict, relevant_id, reason_codes
    )

    customer_reply = sanitize_customer_reply(customer_reply, language)
    next_action = sanitize_internal_text(next_action)
    agent_summary = sanitize_internal_text(agent_summary)

    return {
        "ticket_id": req.ticket_id,
        "relevant_transaction_id": relevant_id,
        "evidence_verdict": verdict,
        "case_type": case_type,
        "severity": severity,
        "department": department,
        "agent_summary": agent_summary,
        "recommended_next_action": next_action,
        "customer_reply": customer_reply,
        "human_review_required": human_review,
        "confidence": confidence,
        "reason_codes": reason_codes,
    }
