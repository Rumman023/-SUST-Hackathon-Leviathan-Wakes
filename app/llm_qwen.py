"""
llm_qwen.py
-----------
OPTIONAL second-opinion auditor and tone-polisher backed by Hugging Face
Inference API (Qwen / Qwen2.5 / Qwen3 family of instruct models).

This module is **strictly advisory**:

1. The structured decisions (transaction match, evidence_verdict, case_type,
   department, severity, escalation, relevant_transaction_id) are ALWAYS
   produced by the deterministic rule engine in `app/reasoning.py`.
2. This module may:
     (a) AUDIT a rule-engine result and emit a `reason_code` ("llm_agrees"
         or "llm_disagrees") plus nudge `confidence` by a small amount.
     (b) POLISH the (already-safe) `customer_reply` for tone.
3. It NEVER overwrites any rule-based decision field.
4. Any network error, timeout, schema violation, or unsafe content from the
   model results in a silent fallback to the rule-only output. The service
   is fully functional with no HF_TOKEN set.

Activation:
    HF_TOKEN=<huggingface token>            # required to enable
    HF_MODEL=Qwen/Qwen2.5-7B-Instruct      # default model
    USE_LLM_PROVIDER=qwen                   # routes llm.py polish hook here
    LLM_AUDIT=1                             # enable second-opinion auditor

Latency budget:
    Default per-call timeout is 6 s (tunable via LLM_TIMEOUT). The reasoning
    engine + auditor + polish stays well inside the 30 s harness limit.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("queuestorm.llm_qwen")

# --- Configuration --------------------------------------------------------

HF_API_BASE = "https://api-inference.huggingface.co/models"
DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"
DEFAULT_TIMEOUT_S = 6.0


def qwen_enabled() -> bool:
    """True iff a Hugging Face token is configured."""
    return bool(os.getenv("HF_TOKEN"))


def auditor_enabled() -> bool:
    """True iff Qwen auditing is enabled (separate flag from polish)."""
    return qwen_enabled() and os.getenv("LLM_AUDIT", "1") == "1"


def polish_enabled() -> bool:
    """True iff Qwen is the chosen polish provider."""
    return (
        qwen_enabled()
        and os.getenv("USE_LLM_PROVIDER", "openai").lower() == "qwen"
    )


def _model_name() -> str:
    return os.getenv("HF_MODEL", DEFAULT_MODEL)


def _timeout() -> float:
    try:
        return float(os.getenv("LLM_TIMEOUT", str(DEFAULT_TIMEOUT_S)))
    except ValueError:
        return DEFAULT_TIMEOUT_S


# --- Prompt builders ------------------------------------------------------

_AUDITOR_SYSTEM = (
    "You are an auditing assistant for a fintech support copilot. You will be "
    "given a customer complaint, the customer's recent transactions, and the "
    "copilot's draft verdict. Decide whether the draft verdict is correct by "
    "comparing the complaint to the transactions. NEVER invent transaction "
    "IDs. NEVER promise refunds, reversals, or account unblocks. NEVER ask "
    "for PIN, OTP, or password. Respond with strict JSON only, with this "
    "exact shape: {\"agree\": true|false, \"reason\": \"<one short sentence>\"}."
)

_POLISH_SYSTEM = (
    "You rewrite a fintech support reply for tone only. Keep it concise, "
    "professional, and in the same language as the input. NEVER ask for "
    "PIN, OTP, password, or card number. NEVER promise a refund, reversal, "
    "or account unblock. Keep any transaction ID that appears in the input. "
    "Return only the rewritten text with no preamble."
)


def _build_audit_user_prompt(
    complaint: str,
    transactions: list,
    draft: Dict[str, Any],
) -> str:
    return json.dumps(
        {
            "complaint": complaint,
            "transactions": transactions,
            "draft_verdict": {
                "relevant_transaction_id": draft.get("relevant_transaction_id"),
                "evidence_verdict": draft.get("evidence_verdict"),
                "case_type": draft.get("case_type"),
            },
            "task": (
                "Does the draft verdict correctly match the complaint to the "
                "transactions? Reply with strict JSON: "
                "{\"agree\": true|false, \"reason\": \"<short>\"}."
            ),
        },
        ensure_ascii=False,
    )


# --- Response sanitisation -----------------------------------------------

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _safe_extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort JSON-object extraction tolerant of model preamble."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    m = _JSON_OBJECT_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (ValueError, TypeError):
        return None


def _bool_agree(parsed: Optional[Dict[str, Any]]) -> Optional[bool]:
    """Normalise the LLM's 'agree' field to a strict bool, or None if ambiguous."""
    if not isinstance(parsed, dict):
        return None
    v = parsed.get("agree")
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1", "agree", "agrees"):
            return True
        if s in ("false", "no", "0", "disagree", "disagrees"):
            return False
    return None


# --- Network call ---------------------------------------------------------

def _call_hf(messages: list, max_tokens: int = 200) -> Optional[str]:
    """
    Call the Hugging Face Inference API (chat completions compatible route).
    Returns the assistant message text, or None on any failure.
    Never raises.
    """
    if not qwen_enabled():
        return None
    url = f"{HF_API_BASE}/{_model_name()}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.environ['HF_TOKEN']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": _model_name(),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,  # deterministic, comparable to the rule engine
        "stream": False,
    }
    try:
        with httpx.Client(timeout=_timeout()) as client:
            r = client.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            logger.warning("hf_inference_non_200 status=%s body=%s",
                           r.status_code, r.text[:200])
            return None
        data = r.json()
        # OpenAI-compatible chat shape
        choices = data.get("choices") or []
        if not choices:
            return None
        msg = choices[0].get("message") or {}
        return (msg.get("content") or "").strip() or None
    except Exception as exc:  # noqa: BLE001 - never fail the request path
        logger.warning("hf_inference_failed err=%s", exc)
        return None


# --- Public API used by main.py / llm.py ----------------------------------

def audit(
    complaint: str,
    transactions: list,
    draft: Dict[str, Any],
) -> Optional[bool]:
    """
    Ask Qwen whether it agrees with the rule-engine draft.

    Returns True / False if the model gave a clean answer, or None if it
    failed, refused, returned non-JSON, or is disabled.
    """
    if not auditor_enabled():
        return None
    text = _call_hf(
        messages=[
            {"role": "system", "content": _AUDITOR_SYSTEM},
            {"role": "user", "content": _build_audit_user_prompt(
                complaint, transactions, draft
            )},
        ],
        max_tokens=120,
    )
    parsed = _safe_extract_json_object(text or "")
    return _bool_agree(parsed)


def maybe_polish_reply(reply: str, language: str = "en") -> str:
    """
    Optional tone-polish through Qwen. Falls back to the rule-based reply on
    any failure or when disabled. Re-sanitisation of the polished text is
    the caller's responsibility (kept here so a misuse is still safe).
    """
    if not polish_enabled():
        return reply
    text = _call_hf(
        messages=[
            {"role": "system", "content": _POLISH_SYSTEM},
            {"role": "user", "content": reply},
        ],
        max_tokens=220,
    )
    if not text:
        return reply
    # Defensive: keep the polished reply the same length order of magnitude.
    if len(text) > 3 * max(len(reply), 60):
        return reply
    return text