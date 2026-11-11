from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from datagen.generate import REFERENCE_DATE, Tables, generate, write_tables
from datagen.schemas import TABLES, column_names, key_column, load_schema

EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$")


def test_rows_have_exactly_the_schema_columns(tables: Tables, schemas: dict[str, dict]) -> None:
    for name in TABLES:
        expected = column_names(schemas[name])
        assert all(list(row) == expected for row in getattr(tables, name))


def test_required_columns_are_never_null(tables: Tables, schemas: dict[str, dict]) -> None:
    for name in TABLES:
        required = [c["name"] for c in schemas[name]["columns"] if c["required"]]
        for row in getattr(tables, name):
            assert all(row[c] is not None for c in required), row


def test_keys_are_unique_and_well_formed(tables: Tables) -> None:
    for name, pattern in [
        ("customers", r"CUS-\d{6}"),
        ("subscriptions", r"SUB-\d{6}"),
        ("invoices", r"INV-\d{7}"),
    ]:
        ids = [row[key_column(load_schema(name))] for row in getattr(tables, name)]
        assert len(ids) == len(set(ids))
        assert all(re.fullmatch(pattern, i) for i in ids)


def test_allowed_values_hold(tables: Tables, schemas: dict[str, dict]) -> None:
    for name in TABLES:
        for column in schemas[name]["columns"]:
            if "allowed_values" in column:
                values = {row[column["name"]] for row in getattr(tables, name)}
                assert values <= set(column["allowed_values"]), (name, column["name"])


def test_customer_rules(tables: Tables) -> None:
    for c in tables.customers:
        assert c["company_name"]
        assert EMAIL.match(c["contact_email"]), c["contact_email"]
        assert c["country"] is None or re.fullmatch(r"[A-Z]{2}", c["country"])
        assert c["signup_date"] <= REFERENCE_DATE
        assert isinstance(c["is_active"], bool)
    # Country is optional, and some nulls must exist so null_variants has something to render.
    assert any(c["country"] is None for c in tables.customers)


def test_subscription_rules(tables: Tables) -> None:
    signup = {c["customer_id"]: c["signup_date"] for c in tables.customers}
    for s in tables.subscriptions:
        assert s["customer_id"] in signup
        assert s["started_at"] >= signup[s["customer_id"]]
        assert (s["canceled_at"] is None) == (s["status"] != "canceled")
        if s["canceled_at"]:
            assert s["canceled_at"] >= s["started_at"]
        assert s["monthly_amount_usd"] >= 0
        assert s["monthly_amount_usd"] == s["monthly_amount_usd"].quantize(Decimal("0.01"))
        assert s["seats"] >= 1


def test_invoice_rules(tables: Tables) -> None:
    owner = {s["subscription_id"]: s["customer_id"] for s in tables.subscriptions}
    for i in tables.invoices:
        assert owner[i["subscription_id"]] == i["customer_id"]
        assert i["due_date"] >= i["invoice_date"]
        assert i["amount_usd"] >= 0
        assert (i["paid_at"] is not None) == (i["status"] == "paid")


def test_volume_ratios_match_spec(tables: Tables) -> None:
    subs_per_customer = len(tables.subscriptions) / len(tables.customers)
    invoices_per_sub = len(tables.invoices) / len(tables.subscriptions)
    assert 1.1 <= subs_per_customer <= 1.3
    assert 4.0 <= invoices_per_sub <= 6.0
    per_customer = Counter(s["customer_id"] for s in tables.subscriptions)
    assert set(per_customer) == {c["customer_id"] for c in tables.customers}


def test_same_seed_gives_identical_output(tmp_path: Path) -> None:
    write_tables(generate(seed=11, customers=150), tmp_path / "a")
    write_tables(generate(seed=11, customers=150), tmp_path / "b")
    con = duckdb.connect()
    for name in TABLES:
        a_csv, b_csv = tmp_path / "a" / f"{name}.csv", tmp_path / "b" / f"{name}.csv"
        assert a_csv.read_bytes() == b_csv.read_bytes()
        a_rows = con.sql(f"select * from '{tmp_path}/a/{name}.parquet'").fetchall()
        b_rows = con.sql(f"select * from '{tmp_path}/b/{name}.parquet'").fetchall()
        assert a_rows == b_rows


def test_different_seeds_differ() -> None:
    assert generate(seed=1, customers=50).customers != generate(seed=2, customers=50).customers


def test_parquet_types_match_schema(tmp_path: Path, schemas: dict[str, dict]) -> None:
    tables = generate(seed=3, customers=60)
    write_tables(tables, tmp_path)
    con = duckdb.connect()
    for name in TABLES:
        described = con.sql(f"describe select * from '{tmp_path}/{name}.parquet'").fetchall()
        actual = [(row[0], row[1].lower()) for row in described]
        expected = [(c["name"], c["type"]) for c in schemas[name]["columns"]]
        assert actual == expected
        [(count,)] = con.sql(f"select count(*) from '{tmp_path}/{name}.parquet'").fetchall()
        assert count == len(getattr(tables, name))


def test_rejects_zero_customers() -> None:
    with pytest.raises(ValueError):
        generate(seed=1, customers=0)
