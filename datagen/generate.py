"""Generate clean, related Acme Cloud tables (customers, subscriptions, invoices).

Output is fully determined by ``--seed`` and ``--customers``. Dates are anchored to a fixed
reference date rather than today, so output does not change from day to day.

Usage: python -m datagen.generate --seed 42 --customers 10000 --out data/clean
"""

from __future__ import annotations

import argparse
import csv
import random
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
from faker import Faker

from datagen.schemas import TABLES, column_names, load_schema

REFERENCE_DATE = date(2025, 6, 30)
FIRST_SIGNUP = date(2019, 1, 1)
LAST_SIGNUP = date(2025, 3, 31)
BILLING_PERIOD_DAYS = 30

COUNTRIES = {
    "US": 40, "GB": 10, "DE": 8, "CA": 7, "FR": 6, "AU": 5, "NL": 4, "IN": 4,
    "BR": 3, "JP": 3, "SG": 2, "ES": 2, "SE": 2, "IE": 2, "MX": 2,
}  # fmt: skip
COUNTRY_NULL_RATE = 0.02
PLANS = {"free": 50, "pro": 35, "enterprise": 15}
PRICE_PER_SEAT = {"free": Decimal("0.00"), "pro": Decimal("25.00"), "enterprise": Decimal("65.00")}
ACTIVE_RATE = 0.85
SECOND_SUBSCRIPTION_RATE = 0.2
LIVE_SUBSCRIPTION_STATUS = {"active": 75, "past_due": 15, "trialing": 10}


@dataclass(frozen=True)
class Tables:
    customers: list[dict[str, Any]]
    subscriptions: list[dict[str, Any]]
    invoices: list[dict[str, Any]]


def _weighted(rng: random.Random, weights: dict[str, int]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def _days(rng: random.Random, low: int, high: int) -> timedelta:
    return timedelta(days=rng.randint(low, high))


def generate(seed: int, customers: int) -> Tables:
    """Build the three tables in memory. Same arguments always give the same rows."""
    if customers < 1:
        raise ValueError("customers must be at least 1")
    rng = random.Random(seed)
    fake = Faker("en_US")
    fake.seed_instance(seed)

    customer_rows: list[dict[str, Any]] = []
    subscription_rows: list[dict[str, Any]] = []
    invoice_rows: list[dict[str, Any]] = []
    signup_span = (LAST_SIGNUP - FIRST_SIGNUP).days

    for c in range(1, customers + 1):
        company = fake.company()
        domain = re.sub(r"[^a-z0-9]", "", company.lower())[:24] + rng.choice([".com", ".io", ".co"])
        customer = {
            "customer_id": f"CUS-{c:06d}",
            "company_name": company,
            "contact_email": f"{fake.user_name()}@{domain}",
            "country": None if rng.random() < COUNTRY_NULL_RATE else _weighted(rng, COUNTRIES),
            "signup_date": FIRST_SIGNUP + timedelta(days=rng.randint(0, signup_span)),
            "plan": _weighted(rng, PLANS),
            "is_active": rng.random() < ACTIVE_RATE,
        }
        customer_rows.append(customer)

        n_subscriptions = 2 if rng.random() < SECOND_SUBSCRIPTION_RATE else 1
        start_floor = customer["signup_date"]
        for s in range(n_subscriptions):
            is_latest = s == n_subscriptions - 1
            plan = customer["plan"] if is_latest else _weighted(rng, PLANS)
            if not is_latest or not customer["is_active"]:
                status = "canceled"
            else:
                status = _weighted(rng, LIVE_SUBSCRIPTION_STATUS)
            started_at = min(start_floor + _days(rng, 0, 30), REFERENCE_DATE)
            periods_available = max(1, (REFERENCE_DATE - started_at).days // BILLING_PERIOD_DAYS)
            n_invoices = 1 if status == "trialing" else min(rng.randint(1, 9), periods_available)
            canceled_at = None
            if status == "canceled":
                last_period = started_at + timedelta(days=BILLING_PERIOD_DAYS * (n_invoices - 1))
                canceled_at = min(last_period + _days(rng, 1, 29), REFERENCE_DATE)
                start_floor = canceled_at

            if plan == "free":
                seats = 1
            elif plan == "pro":
                seats = rng.randint(1, 25)
            else:
                seats = rng.randint(10, 500)
            monthly_amount = (PRICE_PER_SEAT[plan] * seats).quantize(Decimal("0.01"))
            subscription = {
                "subscription_id": f"SUB-{len(subscription_rows) + 1:06d}",
                "customer_id": customer["customer_id"],
                "plan": plan,
                "status": status,
                "started_at": started_at,
                "canceled_at": canceled_at,
                "monthly_amount_usd": monthly_amount,
                "seats": seats,
            }
            subscription_rows.append(subscription)

            for k in range(n_invoices):
                invoice_date = started_at + timedelta(days=BILLING_PERIOD_DAYS * k)
                due_date = invoice_date + timedelta(days=rng.choice([14, 30, 30, 45]))
                invoice_status = _invoice_status(rng, status, is_last=k == n_invoices - 1)
                paid_at = None
                if invoice_status == "paid":
                    paid_at = invoice_date + _days(rng, 0, (due_date - invoice_date).days + 10)
                invoice_rows.append(
                    {
                        "invoice_id": f"INV-{len(invoice_rows) + 1:07d}",
                        "customer_id": customer["customer_id"],
                        "subscription_id": subscription["subscription_id"],
                        "invoice_date": invoice_date,
                        "due_date": due_date,
                        "amount_usd": monthly_amount,
                        "status": invoice_status,
                        "paid_at": paid_at,
                    }
                )

    return Tables(customer_rows, subscription_rows, invoice_rows)


def _invoice_status(rng: random.Random, subscription_status: str, *, is_last: bool) -> str:
    if not is_last:
        return _weighted(rng, {"paid": 90, "void": 4, "uncollectible": 6})
    weights = {
        "active": {"paid": 70, "open": 30},
        "past_due": {"open": 70, "uncollectible": 30},
        "canceled": {"paid": 80, "void": 10, "uncollectible": 10},
        "trialing": {"open": 100},
    }[subscription_status]
    return _weighted(rng, weights)


def format_value(value: Any) -> str:
    """Render one cell for CSV output. Nulls become empty strings."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    return str(value)


def write_csv(path: Path, header: list[str], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow([format_value(row[name]) for name in header])


def write_parquet(csv_path: Path, parquet_path: Path, schema: dict[str, Any]) -> None:
    """Convert the clean CSV to Parquet with the column types declared in the target schema."""
    columns = ", ".join(f"'{c['name']}': '{c['type']}'" for c in schema["columns"])
    con = duckdb.connect()
    try:
        con.execute(
            f"COPY (SELECT * FROM read_csv('{csv_path}', header = true, nullstr = '', "
            f"columns = {{{columns}}})) TO '{parquet_path}' (FORMAT parquet)"
        )
    finally:
        con.close()


def write_tables(tables: Tables, out_dir: Path) -> dict[str, Path]:
    """Write every table as CSV and Parquet; return the CSV paths by table name."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name in TABLES:
        schema = load_schema(name)
        csv_path = out_dir / f"{name}.csv"
        write_csv(csv_path, column_names(schema), getattr(tables, name))
        write_parquet(csv_path, out_dir / f"{name}.parquet", schema)
        paths[name] = csv_path
    return paths


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--customers", type=int, default=10_000)
    parser.add_argument("--out", type=Path, default=Path("data/clean"))
    args = parser.parse_args(argv)

    tables = generate(args.seed, args.customers)
    write_tables(tables, args.out)
    print(
        f"Wrote {len(tables.customers)} customers, {len(tables.subscriptions)} subscriptions, "
        f"{len(tables.invoices)} invoices to {args.out}/"
    )


if __name__ == "__main__":
    main()
