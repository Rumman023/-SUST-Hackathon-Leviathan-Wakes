"""
llm.py
------
OPTIONAL language-polish hook. The structured decisions (transaction match,
verdict, case_type, department, severity, escalation) are ALWAYS made by the
deterministic rule engine in reasoning.py. This module can only rephrase the
already-safe customer_reply for nicer tone, and only when explicitly enabled.

It is disabled unless BOTH are set:
    USE_LLM=1
    OPENAI_API_KEY=<key>   (or a compatible provider key)

If the call fails, times out, or is disabled, the original rule-based text is
returned unchanged. The service therefore works fully with no API key.
"""

from __future__ import annotations

import os

from .safety import sanitize_customer_reply


def llm_enabled() -> bool:
    return os.getenv("USE_LLM", "0") == "1" and bool(os.getenv("OPENAI_API_KEY"))


def maybe_polish_reply(reply: str, language: str = "en") -> str:
    """
    Optionally rephrase the customer reply with an LLM. Returns the original
    text on any error or when disabled. The result is re-sanitised so the LLM
    can never introduce an unsafe promise or credential request.
    """
    if not llm_enabled():
        return reply
    try:
        import httpx  # local import so the dependency is optional at runtime

        model = os.getenv("MODEL_NAME", "gpt-4o-mini")
        system = (
            "You rewrite a fintech support reply for tone only. Keep it concise and "
            "professional. NEVER ask for PIN, OTP, password, or card number. NEVER "
            "promise a refund, reversal, or account unblock. Keep any transaction ID. "
            "Reply in the same language as the input."
        )
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": reply},
                ],
                "temperature": 0.2,
                "max_tokens": 200,
            },
            timeout=float(os.getenv("LLM_TIMEOUT", "8")),
        )
        resp.raise_for_status()
        polished = resp.json()["choices"][0]["message"]["content"].strip()
        if polished:
            # Re-apply guardrails to whatever the model produced.
            return sanitize_customer_reply(polished, language)
    except Exception:
        # Any failure -> fall back to the deterministic, safe reply.
        return reply
    return reply
