"""
audit_smoke.py
--------------
Real end-to-end proof that the Qwen audit path engages on every request,
using the 10 public sample cases from SUST_Preli_Sample_Cases.json.

Two layers of evidence:

  A) MONKEY-PATCHED IN-PROCESS RUN
     Forces HF_TOKEN + LLM_AUDIT, replaces app.llm_qwen._call_hf with a
     recorder, and runs all 10 sample cases through the real pipeline.
     Counts audit calls per case.

  B) LIVE SERVER RUN
     Boots uvicorn on an ephemeral port with HF_TOKEN set, runs all 10
     cases through HTTP, then scrapes the server's stderr log for the
     "hf_inference_failed" / "hf_inference_non_200" warnings emitted by
     app/llm_qwen.py — one per request that actually called the API.

A negative result (no log line per case) means Qwen is NOT being called.
A positive result (one log line per case, or N recorder entries for N
requests) means Qwen IS being called on every request when enabled.

Run:
    python scripts/audit_smoke.py
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List

# Force UTF-8 on Windows so the box characters print cleanly.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
else:  # pragma: no cover
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")  # type: ignore[assignment]
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")  # type: ignore[assignment]

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_samples() -> List[Dict[str, Any]]:
    pack = json.loads((ROOT / "SUST_Preli_Sample_Cases.json").read_text(encoding="utf-8"))
    return [c["input"] for c in pack["cases"]]


# ──────────────────────────────────────────────────────────────────────────
# A) In-process monkey-patch
# ──────────────────────────────────────────────────────────────────────────
@contextmanager
def patched_qwen():
    """Install a fake _call_hf that records every invocation."""
    calls: List[Dict[str, Any]] = []

    def fake_call_hf(messages, max_tokens=200):
        sys_msg = (messages[0].get("content") or "").lower() if messages else ""
        kind = "audit" if "auditing assistant" in sys_msg else \
               "polish" if "rewrite" in sys_msg else "unknown"
        calls.append({"kind": kind, "max_tokens": max_tokens})
        # Always agree in audit, never disagree, so reason_codes flips on.
        return json.dumps({"agree": True, "reason": "fake"})

    if "app.llm_qwen" in sys.modules:
        del sys.modules["app.llm_qwen"]
    if "app.main" in sys.modules:
        del sys.modules["app.main"]
    qwen = importlib.import_module("app.llm_qwen")
    orig = qwen._call_hf
    qwen._call_hf = fake_call_hf  # type: ignore[assignment]
    main = importlib.import_module("app.main")
    orig_main_audit = main.qwen_audit
    main.qwen_audit = qwen.audit  # already patched via module; ensure fresh
    try:
        yield calls
    finally:
        qwen._call_hf = orig  # type: ignore[assignment]
        main.qwen_audit = orig_main_audit  # type: ignore[assignment]
        for m in ("app.main", "app.llm_qwen"):
            sys.modules.pop(m, None)


def run_inprocess() -> Dict[str, Any]:
    samples = load_samples()
    env_before = {k: os.environ.get(k) for k in ("HF_TOKEN", "LLM_AUDIT", "USE_LLM", "USE_LLM_PROVIDER", "OPENAI_API_KEY")}
    os.environ["HF_TOKEN"] = "hf_audit_smoke_test_token"
    os.environ["LLM_AUDIT"] = "1"
    os.environ["USE_LLM"] = "0"            # keep polish off; we only test audit
    os.environ["USE_LLM_PROVIDER"] = "openai"

    try:
        with patched_qwen() as calls:
            main = importlib.import_module("app.main")
            from app.models import AnalyzeRequest

            async def _drive(payload):
                resp = await main._run_analyze(payload)
                return json.loads(resp.body)

            results: List[Dict[str, Any]] = []
            for i, sample in enumerate(samples, 1):
                req = AnalyzeRequest(**sample)
                body = asyncio.run(_drive(req.model_dump()))
                results.append({
                    "case": f"SAMPLE-{i:02d}",
                    "ticket_id": body.get("ticket_id"),
                    "case_type": body.get("case_type"),
                    "evidence_verdict": body.get("evidence_verdict"),
                    "has_audit_code": "llm_audit_agrees" in (body.get("reason_codes") or []),
                })

            return {
                "cases": results,
                "total_calls": len(calls),
                "audit_calls": sum(1 for c in calls if c["kind"] == "audit"),
                "polish_calls": sum(1 for c in calls if c["kind"] == "polish"),
            }
    finally:
        for k, v in env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for m in ("app.main", "app.llm_qwen"):
            sys.modules.pop(m, None)


# ──────────────────────────────────────────────────────────────────────────
# B) Live server
# ──────────────────────────────────────────────────────────────────────────
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_live() -> Dict[str, Any]:
    samples = load_samples()
    port = _free_port()
    log_path = ROOT / ".audit_smoke_server.log"

    env = os.environ.copy()
    env["HF_TOKEN"] = "hf_audit_smoke_live_token"
    env["LLM_AUDIT"] = "1"
    env["USE_LLM"] = "0"
    env["USE_LLM_PROVIDER"] = "openai"
    env["PORT"] = str(port)
    env["LOG_LEVEL"] = "INFO"

    log_fh = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"],
        cwd=str(ROOT), env=env,
        stdout=log_fh, stderr=subprocess.STDOUT,
    )

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.2)
    else:
        proc.terminate(); log_fh.close()
        return {"error": "server did not start in time"}

    responses: List[Dict[str, Any]] = []
    for i, sample in enumerate(samples, 1):
        try:
            req = urllib.request.Request(
                base + "/analyze-ticket",
                data=json.dumps(sample).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.loads(r.read())
            responses.append({
                "case": f"SAMPLE-{i:02d}",
                "ticket_id": body.get("ticket_id"),
                "case_type": body.get("case_type"),
                "status": r.status,
            })
        except Exception as exc:
            responses.append({"case": f"SAMPLE-{i:02d}", "error": str(exc)})

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    log_fh.close()

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    audit_warnings = log_text.count("hf_inference_")  # both failed and non_200
    try:
        log_path.unlink()
    except OSError:
        pass

    return {
        "responses": responses,
        "log_lines_with_hf_call_signal": audit_warnings,
        "expected_one_per_case": len(samples),
    }


# ──────────────────────────────────────────────────────────────────────────
def _print(title: str, payload: Any) -> None:
    print(f"\n── {title} ──")
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def main() -> int:
    print("Qwen audit-path smoke test — 10 public sample cases")
    print("=" * 60)

    a = run_inprocess()
    _print("A) IN-PROCESS (monkey-patched recorder)", a)
    n_cases = len(a.get("cases", []))
    audit_calls = a.get("audit_calls", 0)
    verdict_a = "PASS — audit fires on every case" if audit_calls == n_cases else \
                f"FAIL — expected {n_cases} audit calls, got {audit_calls}"
    print(f"\n  A verdict: {verdict_a}")

    b = run_live()
    _print("B) LIVE SERVER (uvicorn + HF_TOKEN in env)", b)
    expected = b.get("expected_one_per_case", 0)
    got = b.get("log_lines_with_hf_call_signal", 0)
    verdict_b = "PASS — server attempts HF call per case" if got >= expected else \
                f"FAIL — expected ≥{expected} HF call signals, got {got}"
    print(f"\n  B verdict: {verdict_b}")

    print("\n" + "=" * 60)
    print("OVERALL")
    print(f"  A) in-process audit fires per case : {'YES' if audit_calls == n_cases else 'NO'}")
    print(f"  B) live server attempts HF per case: {'YES' if got >= expected else 'NO'}")
    print("=" * 60)
    return 0 if (audit_calls == n_cases and got >= expected) else 1


if __name__ == "__main__":
    raise SystemExit(main())