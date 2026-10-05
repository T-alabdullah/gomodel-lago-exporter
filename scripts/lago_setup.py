"""Configure Lago for token billing (safe to run more than once).

Creates or updates, through Lago's API:

    1. Three billable metrics (sum of input / cached input / output tokens),
       each with a "model" filter so every model can have its own price.
    2. One monthly plan, billed in arrears, with one "package" charge per
       metric: the price is per 1,000,000 tokens.
    3. The test customers and their subscriptions (sub_acme, sub_beta).

Prices and models live in scripts/lago_pricing.json (placeholders for now).
Edit that file and run this script again to change them.

Usage:
    python scripts/lago_setup.py
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from exporter.config import get_settings

PRICING_FILE = Path(__file__).resolve().parent / "lago_pricing.json"

# Must match the test keys in scripts/gomodel_setup.py.
TEST_CUSTOMERS = [
    {"external_id": "acme", "name": "Acme (test)", "subscription_id": "sub_acme"},
    {"external_id": "beta", "name": "Beta (test)", "subscription_id": "sub_beta"},
]


def metric_specs(settings) -> list[dict]:
    """The three metrics. Codes come from exporter config so both sides agree."""
    return [
        {"kind": "input", "code": settings.metric_input,
         "name": "LLM input tokens (uncached)", "field_name": "input_tokens"},
        {"kind": "cached_input", "code": settings.metric_cached_input,
         "name": "LLM cached input tokens", "field_name": "cached_input_tokens"},
        {"kind": "output", "code": settings.metric_output,
         "name": "LLM output tokens", "field_name": "output_tokens"},
    ]


def check(response: httpx.Response, what: str) -> dict:
    """Stop with Lago's error message if a request failed."""
    if response.is_success:
        return response.json()
    sys.exit(f"Failed to {what}: HTTP {response.status_code}\n{response.text}")


def wait_for_lago(client: httpx.Client, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(3)
    sys.exit("Lago did not become healthy. Is it running? (docker compose ps in the lago folder)")


def upsert_metric(client: httpx.Client, spec: dict, models: list[str]) -> dict:
    body = {"billable_metric": {
        "name": spec["name"],
        "code": spec["code"],
        "aggregation_type": "sum_agg",
        "field_name": spec["field_name"],
        "recurring": False,
        "filters": [{"key": "model", "values": models}],
    }}
    if client.get(f"/api/v1/billable_metrics/{spec['code']}").status_code == 404:
        data = check(client.post("/api/v1/billable_metrics", json=body), f"create metric {spec['code']}")
        print(f"  metric {spec['code']}: created")
    else:
        data = check(client.put(f"/api/v1/billable_metrics/{spec['code']}", json=body),
                     f"update metric {spec['code']}")
        print(f"  metric {spec['code']}: updated")
    return data["billable_metric"]


def package(amount: str, package_size: int) -> dict:
    return {"amount": amount, "package_size": package_size, "free_units": 0}


def upsert_plan(client: httpx.Client, pricing: dict, metrics: list[tuple[dict, dict]]) -> None:
    code = pricing["plan"]["code"]
    size = pricing["package_size"]
    existing = client.get(f"/api/v1/plans/{code}")
    # Lago matches charges on update by their id, so look up the existing ones.
    charge_ids = {}
    if existing.status_code == 200:
        for charge in existing.json()["plan"].get("charges", []):
            charge_ids[charge["lago_billable_metric_id"]] = charge["lago_id"]

    charges = []
    for spec, metric in metrics:
        kind = spec["kind"]
        charge = {
            "billable_metric_id": metric["lago_id"],
            "charge_model": "package",
            "pay_in_advance": False,
            "invoiceable": True,
            # Default price, used for any model without its own price below.
            "properties": package(pricing["default_price_per_million_tokens"][kind], size),
            "filters": [
                {"values": {"model": [model]},
                 "invoice_display_name": model,
                 "properties": package(prices[kind], size)}
                for model, prices in pricing["prices_per_million_tokens"].items()
            ],
        }
        if metric["lago_id"] in charge_ids:
            charge["id"] = charge_ids[metric["lago_id"]]
        charges.append(charge)

    body = {"plan": {
        "name": pricing["plan"]["name"],
        "code": code,
        "interval": "monthly",
        "amount_cents": 0,                 # no fixed fee: usage only
        "amount_currency": pricing["currency"],
        "pay_in_advance": False,           # billed in arrears
        "charges": charges,
    }}
    if existing.status_code == 404:
        check(client.post("/api/v1/plans", json=body), f"create plan {code}")
        print(f"  plan {code}: created")
    else:
        check(client.put(f"/api/v1/plans/{code}", json=body), f"update plan {code}")
        print(f"  plan {code}: updated")


def start_of_month_utc() -> str:
    """Midnight UTC on the 1st of this month, e.g. '2026-10-01T00:00:00Z'.

    Lago never bills usage from before a subscription started (decisions.md R4),
    so test subscriptions are back-dated to cover all of this month's test traffic.
    """
    first = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first.strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert_customer_and_subscription(client: httpx.Client, customer: dict, pricing: dict) -> None:
    check(client.post("/api/v1/customers", json={"customer": {
        "external_id": customer["external_id"],
        "name": customer["name"],
        "currency": pricing["currency"],
    }}), f"create customer {customer['external_id']}")
    print(f"  customer {customer['external_id']}: ok")

    sub_id = customer["subscription_id"]
    if client.get(f"/api/v1/subscriptions/{sub_id}").status_code == 200:
        print(f"  subscription {sub_id}: already active")
        return
    check(client.post("/api/v1/subscriptions", json={"subscription": {
        "external_customer_id": customer["external_id"],
        "plan_code": pricing["plan"]["code"],
        "external_id": sub_id,
        "billing_time": "calendar",
        "subscription_at": start_of_month_utc(),
    }}), f"create subscription {sub_id}")
    print(f"  subscription {sub_id}: created")


def main() -> None:
    settings = get_settings()
    api_key = settings.lago_api_key.get_secret_value()
    if not api_key:
        sys.exit("EXPORTER_LAGO_API_KEY is empty in .env (see Step 3.5).")
    pricing = json.loads(PRICING_FILE.read_text())
    models = list(pricing["prices_per_million_tokens"])

    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(base_url=settings.lago_api_url, headers=headers, timeout=30) as client:
        wait_for_lago(client)
        print("Billable metrics")
        metrics = [(spec, upsert_metric(client, spec, models)) for spec in metric_specs(settings)]
        print("Plan")
        upsert_plan(client, pricing, metrics)
        print("Customers and subscriptions")
        for customer in TEST_CUSTOMERS:
            upsert_customer_and_subscription(client, customer, pricing)
    print("Lago is configured.")


if __name__ == "__main__":
    main()