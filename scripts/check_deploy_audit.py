"""Probe the deployed Render service for audit-layer behaviour.

Calls /analyze-ticket 3x and inspects reason_codes / confidence / latency to
help diagnose why llm_audit_agrees may not be appearing in production.
"""
import json
import time
import urllib.request

URL = "https://queuestorm-investigator-ktdw.onrender.com/analyze-ticket"
PAYLOAD = {
    "ticket_id": "TKT-AUDIT-DIAG",
    "complaint": ("I sent 5000 taka to a wrong number around 2pm today, "
                  "please reverse it."),
    "language": "en",
    "transaction_history": [{
        "transaction_id": "TXN-9101",
        "timestamp": "2026-04-14T14:08:22Z",
        "type": "transfer",
        "amount": 5000,
        "counterparty": "+8801719876543",
        "status": "completed",
    }],
}


def call():
    req = urllib.request.Request(
        URL,
        data=json.dumps(PAYLOAD).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t = time.time()
    r = urllib.request.urlopen(req, timeout=60)
    body = json.loads(r.read())
    return round((time.time() - t) * 1000), body


for i in range(1, 4):
    ms, body = call()
    print(f"attempt={i} latency={ms}ms "
          f"confidence={body.get('confidence')} "
          f"reason_codes={body.get('reason_codes')} "
          f"human_review={body.get('human_review_required')} "
          f"verdict={body.get('evidence_verdict')}")
