from __future__ import annotations

import io
import json
import logging
from dataclasses import replace
from pathlib import Path

import mlflow
import pytest
from fastapi.testclient import TestClient

from agent.llm import FakeLLM, FakeTimeout
from api.logs import JsonFormatter, run_context
from api.main import create_app
from api.models import ColumnProfile, Run, RunStatus
from evals.corrupt import write_case
from evals.oracle import oracle_responder

CORRUPTIONS = ["rename_columns", "value_drift", "duplicates", "null_variants"]


@pytest.fixture
def case(tmp_path: Path, clean_rows, schemas) -> tuple[bytes, dict]:
    key = write_case(
        tmp_path / "case", "subs", clean_rows["subscriptions"], schemas["subscriptions"], 2,
        CORRUPTIONS,
    )  # fmt: skip
    return (tmp_path / "case" / "messy.csv").read_bytes(), key


@pytest.fixture
def model() -> dict:
    """The model the API's worker uses; tests swap it between calls."""
    return {"llm": None}


@pytest.fixture
def client(settings, model):
    settings = replace(settings, mlflow_ui_url="http://mlflow.test")
    app = create_app(settings, lambda: model["llm"], inline_worker=True)
    with TestClient(app) as test_client:
        yield test_client


def new_run(client: TestClient, schema: str = "subscriptions") -> str:
    response = client.post("/runs", json={"customer_name": "Acme Cloud", "target_schema": schema})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload(client: TestClient, run_id: str, name: str, content: bytes):
    return client.post(f"/runs/{run_id}/files", files={"files": (name, io.BytesIO(content))})


def test_a_run_completes_through_the_api(client, model, case):
    content, key = case
    model["llm"] = FakeLLM(oracle_responder(key))
    run_id = new_run(client)
    assert client.get(f"/runs/{run_id}").json()["status"] == "created"

    uploaded = upload(client, run_id, "export.csv", content)
    assert uploaded.status_code == 201, uploaded.text
    [file] = uploaded.json()["files"]
    assert (file["filename"], file["row_count"], file["profiled"]) == (
        "export.csv",
        key["messy_rows"],
        False,
    )

    assert client.post(f"/runs/{run_id}/start").status_code == 202
    run = client.get(f"/runs/{run_id}").json()
    assert (run["status"], run["error"], run["steps"]) == ("awaiting_review", None, 2)

    mappings = client.get(f"/runs/{run_id}/mappings").json()
    assert {m["target_column"] for m in mappings} == {m["target_column"] for m in key["mappings"]}
    assert all(m["sample_values"] for m in mappings)
    defects = client.get(f"/runs/{run_id}/defects").json()
    assert sorted(d["defect_type"] for d in defects) == sorted(d["type"] for d in key["defects"])

    pending = [m["id"] for m in mappings if m["status"] == "proposed"]
    decisions = [{"mapping_id": i, "action": "approve"} for i in pending]
    reviewed = client.post(f"/runs/{run_id}/review", json={"decisions": decisions})
    assert reviewed.status_code == 202, reviewed.text
    assert reviewed.json()["building"] is True

    report = client.get(f"/runs/{run_id}/report").json()
    assert report["run"]["status"] == "done", report["run"]["error"]
    [accounting] = report["build"]["row_accounting"]
    assert accounting["balanced"]
    assert (accounting["rows_in"], accounting["filename"]) == (key["messy_rows"], "export.csv")
    assert report["build"]["dbt"]["tests_failed"] == 0
    assert report["model_calls"] == 2
    experiment = mlflow.get_trace(report["trace_id"]).info.experiment_id
    assert report["trace_url"] == (
        f"http://mlflow.test/#/experiments/{experiment}/traces"
        f"?selectedEvaluationId={report['trace_id']}"
    )


@pytest.mark.parametrize(
    ("name", "content", "code", "message"),
    [
        ("export.xlsx", b"x", 415, "unsupported file type .xlsx"),
        ("export.csv", b"", 422, "export.csv is empty"),
        ("export.csv", "id,name\n1,Jos\xe9\n".encode("latin-1"), 422, "is not UTF-8 text"),
        ("export.csv", b"id,name\n", 422, "has a header but no data rows"),
        ("export.parquet", b"not parquet", 422, "not a valid Parquet file"),
    ],
)
def test_bad_uploads_are_rejected_with_readable_messages(
    client, settings, name, content, code, message
):
    run_id = new_run(client)
    response = upload(client, run_id, name, content)
    assert response.status_code == code
    assert message in response.json()["detail"]
    assert client.get(f"/runs/{run_id}").json()["files"] == []
    assert not (settings.data_dir / "runs" / run_id / "uploads" / name).exists()


def test_oversized_uploads_are_rejected(settings, model, tmp_path):
    small = replace(settings, max_upload_mb=1)
    with TestClient(create_app(small, lambda: model["llm"], inline_worker=True)) as client:
        run_id = new_run(client)
        response = upload(client, run_id, "big.csv", b"a,b\n" + b"1,2\n" * 300_000)
    assert response.status_code == 413
    assert response.json()["detail"] == "big.csv is larger than the 1 MB upload limit"


def test_a_failed_run_resumes_from_its_last_completed_phase(client, model, case, settings):
    content, key = case
    run_id = new_run(client)
    upload(client, run_id, "export.csv", content)

    model["llm"] = FakeLLM([FakeTimeout()] * 4)
    client.post(f"/runs/{run_id}/start")
    failed = client.get(f"/runs/{run_id}").json()
    assert failed["status"] == "failed"
    assert "did not respond" in failed["error"]
    assert failed["files"][0]["profiled"] is True

    sessions = client.app.state.sessions
    with sessions() as session:
        profile_ids = sorted(p.id for p in session.query(ColumnProfile))

    model["llm"] = FakeLLM(oracle_responder(key))
    assert client.post(f"/runs/{run_id}/start").status_code == 202
    assert client.get(f"/runs/{run_id}").json()["status"] == "awaiting_review"
    with sessions() as session:
        assert sorted(p.id for p in session.query(ColumnProfile)) == profile_ids  # not redone


def test_requests_out_of_order_are_refused(client, model, case):
    content, key = case
    assert client.get("/runs/nope").status_code == 404
    response = client.post("/runs", json={"customer_name": "x", "target_schema": "orders"})
    assert response.status_code == 422
    assert "Unknown target schema 'orders'" in response.json()["detail"]

    run_id = new_run(client)
    assert (
        client.post(f"/runs/{run_id}/start").json()["detail"].endswith("upload at least one first")
    )
    assert client.post(f"/runs/{run_id}/review", json={}).status_code == 409

    upload(client, run_id, "export.csv", content)
    model["llm"] = FakeLLM(oracle_responder(key))
    client.post(f"/runs/{run_id}/start")
    assert client.post(f"/runs/{run_id}/start").status_code == 409  # waiting for review
    late = upload(client, run_id, "more.csv", content)
    assert late.status_code == 409


def test_review_edits_are_validated(client, model, case):
    content, key = case
    model["llm"] = FakeLLM(oracle_responder(key))
    run_id = new_run(client)
    upload(client, run_id, "export.csv", content)
    client.post(f"/runs/{run_id}/start")
    mapping = client.get(f"/runs/{run_id}/mappings").json()[0]
    bad = {"mapping_id": mapping["id"], "action": "edit", "transform_sql": "DROP TABLE raw.x"}
    response = client.post(f"/runs/{run_id}/review", json={"decisions": [bad], "build": False})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail.startswith(f"Edit to {mapping['target_column']}: ")  # says which edit failed
    assert "Expected a single expression" in detail
    unknown = {"mapping_id": 99999, "action": "approve"}
    response = client.post(f"/runs/{run_id}/review", json={"decisions": [unknown]})
    assert response.status_code == 422


def test_health_reports_each_dependency(client):
    response = client.get("/health")
    body = response.json()
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["tools"] == "ok (in-process)"
    assert body["checks"]["model"].startswith("error:")  # the test settings point nowhere
    assert (response.status_code, body["status"]) == (503, "degraded")


def test_runs_are_listed_newest_first(client):
    first, second = new_run(client), new_run(client, "invoices")
    listed = client.get("/runs").json()
    assert [r["id"] for r in listed] == [second, first]
    assert listed[0]["target_schema"] == "invoices"
    assert [r["id"] for r in client.get("/runs?limit=1").json()] == [second]


def test_only_the_web_app_origin_may_call_the_api_from_a_browser(client):
    def preflight(origin: str):
        return client.options(
            "/runs",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )

    allowed = preflight("http://localhost:3000")
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    refused = preflight("http://evil.example")
    assert "access-control-allow-origin" not in refused.headers


def test_runs_interrupted_by_a_restart_are_marked_failed(settings, model):
    app = create_app(settings, lambda: model["llm"], inline_worker=True)
    with TestClient(app) as client:
        run_id = new_run(client)
        with app.state.sessions() as session:
            session.get(Run, run_id).status = RunStatus.MAPPING
            session.commit()
    with TestClient(create_app(settings, lambda: model["llm"], inline_worker=True)) as client:
        run = client.get(f"/runs/{run_id}").json()
    assert run["status"] == "failed"
    assert run["error"] == f"Interrupted while mapping; POST /runs/{run_id}/start to resume"


def test_log_lines_are_json_with_the_run_id():
    record = logging.LogRecord("agent.loop", logging.INFO, __file__, 1, "step %d", (3,), None)
    with run_context("run-123"):
        entry = json.loads(JsonFormatter().format(record))
    assert (entry["run_id"], entry["message"], entry["level"]) == ("run-123", "step 3", "info")
    assert "run_id" not in json.loads(JsonFormatter().format(record))
