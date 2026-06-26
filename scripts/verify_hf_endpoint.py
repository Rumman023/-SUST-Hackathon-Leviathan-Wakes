"""Standalone probe: hit the same HF endpoint that llm_qwen._call_hf uses.

Useful for diagnosing "audit silently skips" without spinning up the server.
Reads HF_TOKEN from the local environment (or .env) and prints status, body
excerpt, and whether the response looks like a valid chat completion.

Usage:
    python scripts/verify_hf_endpoint.py
"""
from __future__ import annotations

import json
import os
import sys

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001 - dotenv is optional here
    pass

MODEL = os.getenv("HF_MODEL", "Qwen/Qwen2.5-7B-Instruct")
TOKEN = os.getenv("HF_TOKEN", "")
URL = f"https://api-inference.huggingface.co/models/{MODEL}/v1/chat/completions"


def main() -> int:
    if not TOKEN:
        print("ERROR: HF_TOKEN is empty in the local environment.")
        print("Set it in .env or export HF_TOKEN=hf_... and re-run.")
        return 2

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "Reply with strict JSON: {\"agree\": true}"},
            {"role": "user", "content": "ping"},
        ],
        "max_tokens": 20,
        "temperature": 0.0,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }
    print(f"POST {URL}")
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(URL, headers=headers, json=payload)
    except Exception as exc:  # noqa: BLE001
        print(f"NETWORK ERROR: {exc!r}")
        return 1

    print(f"status={r.status_code}")
    body = r.text
    print(f"body[:400]={body[:400]!r}")
    if r.status_code != 200:
        print("NOT-OK: HF did not return 200. The auditor will silently skip.")
        return 1
    try:
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"PARSE ERROR: {exc!r} (HF returned non-JSON)")
        return 1
    choices = data.get("choices") or []
    if not choices:
        print("OK-200 but no choices[]. The auditor will silently skip.")
        return 1
    content = (choices[0].get("message") or {}).get("content")
    print(f"OK-200 choices[0].message.content={content!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())