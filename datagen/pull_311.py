"""Fetch one day of NYC 311 service requests and submit it to the running API.

Usage: python -m datagen.pull_311 [--date YYYY-MM-DD] [--api URL] [--out-dir DIR] [--limit N]

The first run starts an onboarding run for review; once it is saved as the recurring
source "nyc-311", later runs submit each day as a recurring load.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path
from typing import Any

import httpx

SOCRATA_URL = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"
RAW_COLUMNS = (
    "unique_key",
    "created_date",
    "closed_date",
    "agency",
    "complaint_type",
    "descriptor",
    "status",
    "borough",
    "incident_zip",
    "latitude",
    "longitude",
)
PAGE_SIZE = 10_000
TIMEOUT = 60.0
CUSTOMER_NAME = "NYC Open Data"
SOURCE_NAME = "nyc-311"
TARGET_SCHEMA = "service_requests"
WEB_URL = "http://localhost:3000"


class ApiError(Exception):
    """The API could not be reached or answered with an error."""


def fetch_day(
    client: httpx.Client, date: dt.date, limit: int, page_size: int = PAGE_SIZE
) -> list[dict[str, Any]]:
    """Return up to ``limit`` 311 records created on ``date``, ordered by unique_key."""
    day = date.isoformat()
    rows: list[dict[str, Any]] = []
    while len(rows) < limit:
        size = min(page_size, limit - len(rows))
        params = {
            "$select": ",".join(RAW_COLUMNS),
            "$where": f"created_date between '{day}T00:00:00' and '{day}T23:59:59.999'",
            "$order": "unique_key",
            "$limit": str(size),
            "$offset": str(len(rows)),
        }
        response = client.get(SOCRATA_URL, params=params)
        response.raise_for_status()
        page = response.json()
        rows.extend(page)
        if len(page) < size:
            break
    return rows


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(RAW_COLUMNS)
        for row in rows:
            writer.writerow([row.get(column, "") for column in RAW_COLUMNS])


def _call(client: httpx.Client, method: str, url: str, **kwargs: Any) -> Any:
    try:
        response = client.request(method, url, **kwargs)
    except httpx.TransportError as exc:
        raise ApiError(
            f"Could not reach the API at {client.base_url} ({exc}). Is the stack running (make up)?"
        ) from exc
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise ApiError(f"{method} {url} failed with {response.status_code}: {detail}")
    return response.json()


def submit(client: httpx.Client, path: Path) -> str:
    """Submit ``path`` to the API; return the lines to print for the user."""
    contracts = _call(client, "GET", "/contracts")
    source = next(
        (
            c
            for c in contracts
            if c["customer_name"] == CUSTOMER_NAME and c["source_name"] == SOURCE_NAME
        ),
        None,
    )
    content = path.read_bytes()
    if source is not None:
        run = _call(
            client,
            "POST",
            f"/contracts/{source['id']}/loads",
            files={"file": (path.name, content, "text/csv")},
        )
        return f"Started recurring load {run['id']}\nFollow it at {WEB_URL}/runs/{run['id']}"
    run = _call(
        client,
        "POST",
        "/runs",
        json={"customer_name": CUSTOMER_NAME, "target_schema": TARGET_SCHEMA},
    )
    run_id = run["id"]
    _call(
        client, "POST", f"/runs/{run_id}/files", files={"files": (path.name, content, "text/csv")}
    )
    _call(client, "POST", f"/runs/{run_id}/start")
    return "\n".join(
        [
            f"No recurring source '{SOURCE_NAME}' yet, so this is the first load.",
            f"Started onboarding run {run_id}.",
            f"1. Review it at {WEB_URL}/runs/{run_id}",
            f"2. When it is done, save it as a recurring source named '{SOURCE_NAME}'"
            " from that page",
            f'   (or POST /runs/{run_id}/contract with {{"source_name": "{SOURCE_NAME}"}}).',
            "3. Run `make pull-311` again for the next day.",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    yesterday = dt.date.today() - dt.timedelta(days=1)
    parser.add_argument("--date", type=dt.date.fromisoformat, default=yesterday)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--out-dir", type=Path, default=Path("data/311"))
    parser.add_argument("--limit", type=int, default=50_000)
    args = parser.parse_args(argv)

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            rows = fetch_day(client, args.date, args.limit)
    except httpx.HTTPError as exc:
        print(f"Could not fetch 311 data from NYC Open Data: {exc}", file=sys.stderr)
        return 1
    path = args.out_dir / f"nyc311_{args.date.isoformat()}.csv"
    write_csv(rows, path)
    print(f"Wrote {len(rows)} rows to {path}")

    try:
        with httpx.Client(base_url=args.api, timeout=TIMEOUT) as client:
            print(submit(client, path))
    except ApiError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
