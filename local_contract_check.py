#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


BOT_URL = "http://127.0.0.1:8080"
ROOT = Path(__file__).resolve().parent


def request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BOT_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            print(f"{method} {path} -> {resp.status}")
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            print()
            return payload
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode("utf-8"))
        print(f"{method} {path} -> {exc.code}")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        print()
        return payload


def load_seed_items() -> tuple[dict, dict, dict, dict]:
    category = json.loads((ROOT / "dataset" / "categories" / "dentists.json").read_text())
    merchants = json.loads((ROOT / "dataset" / "merchants_seed.json").read_text())["merchants"]
    customers = json.loads((ROOT / "dataset" / "customers_seed.json").read_text())["customers"]
    triggers = json.loads((ROOT / "dataset" / "triggers_seed.json").read_text())["triggers"]

    merchant = next(item for item in merchants if item["merchant_id"] == "m_001_drmeera_dentist_delhi")
    customer = next(item for item in customers if item["customer_id"] == "c_001_priya_for_m001")
    merchant_trigger = next(item for item in triggers if item["id"] == "trg_001_research_digest_dentists")
    customer_trigger = next(item for item in triggers if item["id"] == "trg_003_recall_due_priya")
    return category, merchant, customer, merchant_trigger, customer_trigger


def main() -> int:
    try:
        request("GET", "/v1/healthz")
        request("GET", "/v1/metadata")
    except Exception as exc:
        print(f"Bot not reachable at {BOT_URL}: {exc}", file=sys.stderr)
        return 1

    category, merchant, customer, merchant_trigger, customer_trigger = load_seed_items()

    request(
        "POST",
        "/v1/context",
        {
            "scope": "category",
            "context_id": "dentists",
            "version": 10,
            "payload": category,
            "delivered_at": "2026-05-03T10:00:00Z",
        },
    )
    request(
        "POST",
        "/v1/context",
        {
            "scope": "merchant",
            "context_id": merchant["merchant_id"],
            "version": 10,
            "payload": merchant,
            "delivered_at": "2026-05-03T10:01:00Z",
        },
    )
    request(
        "POST",
        "/v1/context",
        {
            "scope": "customer",
            "context_id": customer["customer_id"],
            "version": 10,
            "payload": customer,
            "delivered_at": "2026-05-03T10:02:00Z",
        },
    )
    request(
        "POST",
        "/v1/context",
        {
            "scope": "trigger",
            "context_id": merchant_trigger["id"],
            "version": 10,
            "payload": merchant_trigger,
            "delivered_at": "2026-05-03T10:03:00Z",
        },
    )
    request(
        "POST",
        "/v1/context",
        {
            "scope": "trigger",
            "context_id": customer_trigger["id"],
            "version": 10,
            "payload": customer_trigger,
            "delivered_at": "2026-05-03T10:04:00Z",
        },
    )

    merchant_tick = request(
        "POST",
        "/v1/tick",
        {
            "now": "2026-05-02T10:30:00Z",
            "available_triggers": [merchant_trigger["id"]],
        },
    )
    merchant_conv = merchant_tick["actions"][0]["conversation_id"]

    request(
        "POST",
        "/v1/reply",
        {
            "conversation_id": merchant_conv,
            "merchant_id": merchant["merchant_id"],
            "customer_id": None,
            "from_role": "merchant",
            "message": "Ok lets do it. Whats next?",
            "received_at": "2026-05-02T10:35:00Z",
            "turn_number": 2,
        },
    )

    request(
        "POST",
        "/v1/reply",
        {
            "conversation_id": "conv_auto_local",
            "merchant_id": merchant["merchant_id"],
            "customer_id": None,
            "from_role": "merchant",
            "message": "Thank you for contacting us! Our team will respond shortly.",
            "received_at": "2026-05-02T10:36:00Z",
            "turn_number": 2,
        },
    )

    request(
        "POST",
        "/v1/reply",
        {
            "conversation_id": "conv_hostile_local",
            "merchant_id": merchant["merchant_id"],
            "customer_id": None,
            "from_role": "merchant",
            "message": "Stop messaging me. This is useless spam.",
            "received_at": "2026-05-02T10:37:00Z",
            "turn_number": 2,
        },
    )

    customer_tick = request(
        "POST",
        "/v1/tick",
        {
            "now": "2026-11-03T10:30:00Z",
            "available_triggers": [customer_trigger["id"]],
        },
    )
    customer_conv = customer_tick["actions"][0]["conversation_id"]

    request(
        "POST",
        "/v1/reply",
        {
            "conversation_id": customer_conv,
            "merchant_id": merchant["merchant_id"],
            "customer_id": customer["customer_id"],
            "from_role": "customer",
            "message": "1",
            "received_at": "2026-11-03T10:35:00Z",
            "turn_number": 2,
        },
    )

    print("Local contract check completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
