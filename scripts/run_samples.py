#!/usr/bin/env python3
"""
run_samples.py
--------------
Run the 10 public sample cases and print a functional-equivalence report.

Two modes:
  * In-process (default): imports the reasoning engine directly. No server
    needed.  ->  python scripts/run_samples.py
  * Live URL: hits a deployed endpoint.
        python scripts/run_samples.py --url https://your-service.com

Compares the automatically-scored fields against each case's expected_output
and flags any unsafe customer_reply.
"""

import argparse
import json
import os
import sys

# Make the package importable when run from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EXACT_FIELDS = ["relevant_transaction_id", "evidence_verdict", "case_type", "department"]


def load_cases(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["cases"]


def analyze_in_process(payload):
    from app.models import AnalyzeRequest
    from app.reasoning import analyze
    return analyze(AnalyzeRequest.model_validate(payload))


def analyze_live(url, payload):
    import httpx
    r = httpx.post(url.rstrip("/") + "/analyze-ticket", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def is_safe(reply):
    from app.safety import reply_is_safe
    return reply_is_safe(reply)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="Live base URL; omit to run in-process.")
    ap.add_argument("--file", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "SUST_Preli_Sample_Cases.json"))
    args = ap.parse_args()

    cases = load_cases(args.file)
    passed = 0
    print(f"Running {len(cases)} sample cases "
          f"({'live: ' + args.url if args.url else 'in-process'})\n")

    for case in cases:
        cid = case["id"]
        expected = case["expected_output"]
        try:
            out = analyze_live(args.url, case["input"]) if args.url else \
                analyze_in_process(case["input"])
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] {cid}: {exc}")
            continue

        diffs = []
        for f in EXACT_FIELDS + ["severity", "human_review_required"]:
            if out.get(f) != expected.get(f):
                diffs.append(f"{f}: got {out.get(f)!r} != {expected.get(f)!r}")
        if not is_safe(out.get("customer_reply", "")):
            diffs.append("UNSAFE customer_reply")

        if diffs:
            print(f"[FAIL] {cid} ({case['label']})")
            for d in diffs:
                print(f"        - {d}")
        else:
            passed += 1
            print(f"[PASS] {cid} ({case['label']})")

    print(f"\n{passed}/{len(cases)} cases functionally equivalent.")
    sys.exit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    main()
