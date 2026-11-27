from __future__ import annotations

import pytest

from datagen.schemas import load_schema
from tools.dbt_runner import (
    SourceFile,
    SourceMapping,
    get_dbt_errors,
    render_project,
    row_accounting,
    run_dbt,
)
from tools.warehouse import ToolError, get_file, source_columns

CSV = """\
Cust,Company,Email,Ctry,Joined,Tier,Active
cus-000001,Acme,a@x.com,US,2024-01-05,pro,true
CUS-000002,Beta,b@x.com,,2024-02-30,free,true
CUS-000003,Gamma,c@x.com,GB,2024-03-01,,false
CUS-000001,Acme again,a@x.com,US,2024-01-05,pro,true
CUS-000004,Delta,d@x.com,N/A,2024-04-01,enterprise,yes
CUS-000005,Eps,e@x.com,FR,2024-05-01,PRO,Y
"""

GOOD = {
    "customer_id": ("Cust", 'upper(trim("Cust"))'),
    "company_name": ("Company", 'trim("Company")'),
    "contact_email": ("Email", 'trim("Email")'),
    "country": (
        "Ctry",
        "CASE WHEN lower(trim(\"Ctry\")) IN ('n/a', '') THEN NULL ELSE trim(\"Ctry\") END",
    ),
    "signup_date": ("Joined", 'try_cast("Joined" AS DATE)'),
    "plan": ("Tier", 'lower(trim("Tier"))'),
    "is_active": (
        "Active",
        "CASE WHEN lower(\"Active\") IN ('true', 'yes', 'y') THEN true "
        "WHEN lower(\"Active\") IN ('false', 'no', 'n') THEN false END",
    ),
}  # fmt: skip


@pytest.fixture
def build(wh, load_csv):
    file = load_csv(CSV, name="export.csv")
    with wh.connect(read_only=True) as con:
        columns = source_columns(con, get_file(con, file.file_id))
    schema = load_schema("customers")

    def render(**overrides: str) -> str:
        transforms = {
            target: (src, overrides.get(target, sql)) for target, (src, sql) in GOOD.items()
        }
        mappings = [SourceMapping(t, src, sql, False) for t, (src, sql) in transforms.items()]
        render_project(
            wh, "test-run", schema, [SourceFile(file.file_id, file.table_name, columns, mappings)]
        )
        return file.file_id

    return render


def loaded_rows(wh) -> list[tuple]:
    with wh.connect(read_only=True) as con:
        return con.execute(
            "SELECT customer_id, plan, is_active, country, _source_row FROM main.stg_customers "
            "ORDER BY _source_row"
        ).fetchall()


def test_bad_rows_are_quarantined_with_reasons_and_every_row_is_accounted_for(wh, build):
    file_id = build()
    result = run_dbt(wh)
    assert result.success, result
    assert (result.models_passed, result.models_failed, result.tests_failed) == (3, 0, 0)
    assert result.tests_passed == 8  # 6 not_null, 1 unique, 1 accepted_values

    assert loaded_rows(wh) == [
        ("CUS-000001", "pro", True, "US", 1),
        ("CUS-000004", "enterprise", True, None, 5),
        ("CUS-000005", "pro", True, "FR", 6),
    ]
    [accounting] = row_accounting(wh, "customers", [file_id])
    assert (accounting.rows_in, accounting.loaded, accounting.rejected) == (6, 3, 3)
    assert accounting.balanced
    assert accounting.reject_reasons == {
        "could not convert signup_date": 1,
        "missing required plan": 1,
        "duplicate customer_id": 1,
    }
    with wh.connect(read_only=True) as con:
        [(raw,)] = con.execute(
            "SELECT _raw_values FROM main.rejected_customers WHERE _source_row = 2"
        ).fetchall()
    assert '"Joined":"2024-02-30"' in raw  # quarantined rows keep their original values


def test_values_outside_the_allowed_set_fail_the_build_with_failing_rows(wh, build):
    build(plan='trim("Tier")')  # leaves "PRO" as is
    result = run_dbt(wh)
    assert not result.success
    assert result.tests_failed == 1
    [failure] = get_dbt_errors(wh)
    assert failure.kind == "test"
    assert failure.name.startswith("accepted_values_stg_customers_plan")
    assert failure.failing_rows == [{"value_field": "PRO", "n_records": 1}]


def test_runtime_errors_in_a_transform_fail_the_model(wh, build):
    build(signup_date='CAST("Joined" AS DATE)')  # 2024-02-30 cannot be cast
    result = run_dbt(wh)
    assert not result.success
    assert result.models_failed >= 1
    failed = get_dbt_errors(wh)
    assert any(f.kind == "model" and "Conversion Error" in (f.message or "") for f in failed)


def test_rebuilding_replaces_tables_instead_of_appending(wh, build):
    file_id = build()
    run_dbt(wh)
    run_dbt(wh)
    assert len(loaded_rows(wh)) == 3
    [accounting] = row_accounting(wh, "customers", [file_id])
    assert (accounting.loaded, accounting.rejected) == (3, 3)


def test_render_refuses_missing_mappings_and_raw_breakouts(wh, build):
    with pytest.raises(ToolError, match="No approved mappings"):
        render_project(wh, "test-run", load_schema("customers"), [])
    with pytest.raises(ToolError, match="reserved word"):
        build(company_name="'{% endraw %}'")


def test_dbt_errors_before_any_build_are_tool_errors(wh, load_csv):
    load_csv(CSV)
    with pytest.raises(ToolError, match="has not been rendered"):
        run_dbt(wh)
    with pytest.raises(ToolError, match="dbt has not run yet"):
        get_dbt_errors(wh)
