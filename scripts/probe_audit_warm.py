"""One-shot warm-up probe: 4 sequential calls to surface HF cold-start.

Why: the first call after a free-tier HF model goes to sleep returns 503
"model is loading" in a few seconds. If our audit layer is *attempting* the
call but failing silently, we'd see one slow call followed by fast ones.
"""
from __future__ import annotations

import time

import httpx

URL = "https://queuestorm-investigator-ktdw.onrender.com/analyze-ticket"
BODY = {
    "ticket_id": "AUDIT-PROBE-1",
    "complaint": "I sent 5000 taka to a wrong number at 2pm today, please reverse it.",
    "language": "en",
    "transaction_history": [
        {
            "transaction_id": "TXN-9101",
            "timestamp": "2026-04-14T14:08:22Z",
            "type": "transfer",
            "amount": 5000,
            "counterparty": "+8801719876543",
            "status": "completed",
        }
    ],
}


def main() -> None:
    with httpx.Client(timeout=30.0) as client:
        for i in range(4):
            t0 = time.perf_counter()
            try:
                r = client.post(URL, json=BODY)
                dt = (time.perf_counter() - t0) * 1000
                j = r.json()
                rc = j.get("reason_codes")
                conf = j.get("confidence")
                verdict = j.get("evidence_verdict")
                has_audit = isinstance(rc, list) and any(
                    str(c).startswith("llm_audit_") for c in rc
                )
                tag = "AUDIT-FIRED" if has_audit else "rule-only"
                print(
                    f"attempt={i+1} status={r.status_code} "
                    f"latency={dt:6.0f}ms reason_codes={rc} "
                    f"confidence={conf} verdict={verdict} -> {tag}"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"attempt={i+1} ERROR {exc!r}")


if __name__ == "__main__":
    main()
