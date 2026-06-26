"""
main.py
-------
FastAPI service for the QueueStorm Investigator.

Endpoints:
    GET  /health          -> {"status": "ok"}
    POST /analyze-ticket  -> structured analysis (Section 6 schema)

Status-code contract (Section 4.1):
    200  valid request, schema-conformant body
    400  malformed JSON or missing/invalid required fields
    422  schema valid but semantically invalid (empty complaint)
    500  internal error (safe message only; never a stack trace or secret)

The service binds 0.0.0.0:$PORT (default 8000) and never crashes on bad input.
"""

from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv

# Load variables from a local `.env` file if present. This is a no-op when
# the file does not exist. Existing process environment variables always
# win over the file, so production overrides are honoured.
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .llm import llm_enabled, maybe_polish_reply as polish_openai
from .llm_qwen import (
    auditor_enabled,
    maybe_polish_reply as polish_qwen,
    audit as qwen_audit,
)
from .models import AnalyzeRequest, AnalyzeResponse
from .reasoning import analyze
from .utils import detect_language

logger = logging.getLogger("queuestorm")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# --- startup env visibility (booleans only; never log secret values) -------
# Lets you `grep env_audit_visibility` in Render logs to confirm the
# container actually sees HF_TOKEN + LLM_AUDIT before any request runs.
logger.info(
    "env_audit_visibility HF_TOKEN_set=%s LLM_AUDIT=%s "
    "USE_LLM=%s USE_LLM_PROVIDER=%s HF_MODEL=%s OPENAI_API_KEY_set=%s",
    bool(os.getenv("HF_TOKEN")),
    os.getenv("LLM_AUDIT", "1"),
    os.getenv("USE_LLM", "0"),
    os.getenv("USE_LLM_PROVIDER", "openai"),
    os.getenv("HF_MODEL", "(default)"),
    bool(os.getenv("OPENAI_API_KEY")),
)

# Hard ceiling on request body (32 KB is generous for a single complaint +
# a short transaction snippet, and prevents a hostile client from locking a
# worker). Tunable via env var.
REQUEST_MAX_BYTES = int(os.getenv("REQUEST_MAX_BYTES", str(32 * 1024)))

# Per-request wall-clock cap. Stays well inside the harness's 30 s budget.
REQUEST_TIMEOUT_S = float(os.getenv("REQUEST_TIMEOUT_S", "25"))

app = FastAPI(
    title="QueueStorm Investigator",
    description="Evidence-grounded support copilot for digital finance complaints.",
    version="1.0.0",
)


def _error(status: int, message: str) -> JSONResponse:
    """Uniform, non-sensitive error envelope."""
    return JSONResponse(status_code=status, content={"error": message})


def _polish(reply: str, language: str) -> str:
    """Route polish to whichever provider is configured. Falls back safely."""
    provider = os.getenv("USE_LLM_PROVIDER", "openai").lower()
    if provider == "qwen":
        return polish_qwen(reply, language)
    return polish_openai(reply, language)


@app.get("/health")
async def health():
    return {"status": "ok"}


async def _run_analyze(payload: dict) -> JSONResponse:
    """The full pipeline, executed under a timeout so a misbehaving LLM
    call (or any unexpected hang) cannot breach the 30 s harness budget."""
    # ---- 1. Required-field checks (-> 400) ------------------------------
    ticket_id = payload.get("ticket_id")
    complaint = payload.get("complaint")
    if not isinstance(ticket_id, str) or ticket_id.strip() == "":
        return _error(400, "Missing or invalid required field: ticket_id.")
    if "complaint" not in payload or not isinstance(complaint, str):
        return _error(400, "Missing or invalid required field: complaint.")

    # ---- 2. Semantic validation (empty complaint -> 422) ----------------
    if complaint.strip() == "":
        return _error(422, "Field 'complaint' must not be empty.")

    # ---- 3. Build a lenient request model (type issues -> 400) ----------
    try:
        req = AnalyzeRequest.model_validate(payload)
    except ValidationError:
        # Retry with optional fields dropped so a single bad optional value
        # cannot fail an otherwise valid request.
        safe_payload = {"ticket_id": ticket_id, "complaint": complaint}
        for key in ("language", "channel", "user_type", "campaign_context", "metadata"):
            if key in payload:
                safe_payload[key] = payload[key]
        th = payload.get("transaction_history")
        if isinstance(th, list):
            safe_payload["transaction_history"] = th
        try:
            req = AnalyzeRequest.model_validate(safe_payload)
        except ValidationError:
            req = AnalyzeRequest(ticket_id=ticket_id, complaint=complaint)

    # ---- 4. Investigate (any unexpected error -> safe 500) --------------
    try:
        result = analyze(req)

        # Optional LLM polish of the (already safe) customer reply.
        if llm_enabled():
            language = detect_language(req.complaint, req.language)
            result["customer_reply"] = _polish(result["customer_reply"], language)

        # Optional Qwen second-opinion audit. Never overwrites the rule
        # engine's decisions; only adds a reason_code and a small confidence
        # nudge so the agent can see the model weighed in.
        if auditor_enabled():
            try:
                txns = [t.model_dump() for t in (req.transaction_history or [])]
                verdict = qwen_audit(req.complaint, txns, result)
                if verdict is True:
                    result["reason_codes"] = list(result.get("reason_codes") or []) + [
                        "llm_audit_agrees"
                    ]
                    result["confidence"] = min(
                        1.0, float(result.get("confidence") or 0.7) + 0.03
                    )
                elif verdict is False:
                    result["reason_codes"] = list(result.get("reason_codes") or []) + [
                        "llm_audit_disagrees"
                    ]
                    # On disagreement, force human review rather than override.
                    result["human_review_required"] = True
                    result["confidence"] = max(
                        0.5, float(result.get("confidence") or 0.7) - 0.10
                    )
            except Exception as exc:  # noqa: BLE001 - audit must never break
                logger.warning("qwen_audit_failed err=%s", exc)

        # Final schema enforcement: build through the strict response model so
        # we can never emit an out-of-vocabulary enum or extra field.
        validated = AnalyzeResponse(**result)
        return JSONResponse(status_code=200, content=validated.model_dump())
    except Exception as exc:  # noqa: BLE001 - we deliberately swallow + log
        logger.exception("analysis_failed ticket_id=%s", payload.get("ticket_id"))
        _ = exc  # avoid leaking details to the client
        return _error(500, "Internal error while analyzing the ticket.")


@app.post("/analyze-ticket")
async def analyze_ticket(request: Request):
    # ---- 0. Body size guard -------------------------------------------
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > REQUEST_MAX_BYTES:
                return _error(400, "Request body too large.")
        except ValueError:
            pass

    # ---- 1. Parse JSON (malformed -> 400) ------------------------------
    try:
        payload = await request.json()
    except Exception:
        return _error(400, "Malformed JSON body.")

    if not isinstance(payload, dict):
        return _error(400, "Request body must be a JSON object.")

    # ---- 2. Run pipeline under a wall-clock cap ------------------------
    try:
        return await asyncio.wait_for(_run_analyze(payload), timeout=REQUEST_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.error("analyze_timeout ticket_id=%s", payload.get("ticket_id"))
        return _error(500, "Analysis timed out.")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)
