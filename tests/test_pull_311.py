import csv
import datetime as dt
import json

import httpx
import pytest

from datagen.pull_311 import RAW_COLUMNS, ApiError, fetch_day, submit, write_csv


def test_fetch_pages_and_write_csv(tmp_path):
    offsets = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        offsets.append(params["$offset"])
        assert params["$order"] == "unique_key"
        assert "2026-11-25T00:00:00" in params["$where"]
        if params["$offset"] == "0":
            page = [{"unique_key": "1", "agency": "NYPD"}, {"unique_key": "2", "agency": "DOT"}]
        else:
            page = [{"unique_key": "3", "status": "Closed"}]
        return httpx.Response(200, json=page)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = fetch_day(client, dt.date(2026, 11, 25), limit=100, page_size=2)
    assert offsets == ["0", "2"]
    path = tmp_path / "out.csv"
    write_csv(rows, path)
    with path.open() as fh:
        data = list(csv.reader(fh))
    assert data[0] == list(RAW_COLUMNS)
    assert len(data) == 4
    assert data[3][0] == "3" and data[3][RAW_COLUMNS.index("status")] == "Closed"
    assert data[3][RAW_COLUMNS.index("agency")] == ""


def _api(routes, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        calls.append(key)
        status, body = routes[key]
        return httpx.Response(status, json=body)

    return httpx.Client(base_url="http://api", transport=httpx.MockTransport(handler))


def _file(tmp_path):
    path = tmp_path / "nyc311_2026-11-25.csv"
    path.write_text("unique_key\n1\n")
    return path


def test_submit_existing_source_posts_load(tmp_path):
    calls = []
    contracts = [
        {"id": 3, "customer_name": "Other", "source_name": "nyc-311"},
        {"id": 7, "customer_name": "NYC Open Data", "source_name": "nyc-311"},
    ]
    client = _api(
        {"GET /contracts": (200, contracts), "POST /contracts/7/loads": (202, {"id": "r1"})},
        calls,
    )
    out = submit(client, _file(tmp_path))
    assert calls == ["GET /contracts", "POST /contracts/7/loads"]
    assert "http://localhost:3000/runs/r1" in out


def test_submit_first_load_creates_onboarding_run(tmp_path):
    calls = []
    bodies = []
    routes = {
        "GET /contracts": (200, []),
        "POST /runs": (201, {"id": "r2"}),
        "POST /runs/r2/files": (200, {"id": "r2"}),
        "POST /runs/r2/start": (202, {"id": "r2"}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        calls.append(key)
        if key == "POST /runs":
            bodies.append(json.loads(request.content))
        if key == "POST /runs/r2/files":
            assert b'name="files"' in request.content
        status, body = routes[key]
        return httpx.Response(status, json=body)

    client = httpx.Client(base_url="http://api", transport=httpx.MockTransport(handler))
    out = submit(client, _file(tmp_path))
    assert calls == ["GET /contracts", "POST /runs", "POST /runs/r2/files", "POST /runs/r2/start"]
    assert bodies == [{"customer_name": "NYC Open Data", "target_schema": "service_requests"}]
    assert "/runs/r2/contract" in out and "nyc-311" in out


def test_submit_api_error_shows_detail(tmp_path):
    calls = []
    client = _api(
        {
            "GET /contracts": (
                200,
                [{"id": 7, "customer_name": "NYC Open Data", "source_name": "nyc-311"}],
            ),
            "POST /contracts/7/loads": (422, {"detail": "The file is empty."}),
        },
        calls,
    )
    with pytest.raises(ApiError, match=r"422: The file is empty\."):
        submit(client, _file(tmp_path))
