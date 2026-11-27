from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import anyio
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from mcp.client import Client

import agent.runner
import tools.server as tools_server
from agent.llm import FakeLLM
from agent.tool_backend import LocalTools, McpTools
from agent.tracing import Tracer
from api.db import make_engine, migrate, session_factory
from api.models import Base, RunStatus
from evals.corrupt import write_case
from evals.oracle import answer_key_reviewer, oracle_responder
from tools.warehouse import ToolError, Warehouse, load_file


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch) -> Path:
    """The MCP server finds warehouses under DATA_DIR, like the tools container."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture
def loaded(data_dir, tmp_path):
    path = tmp_path / "export.csv"
    path.write_text("id,Joined,Tier\n1,2024-01-05,pro\n2,03/01/2024,PRO\n3,,free\n")
    wh = Warehouse.for_run("mcp-run", data_dir)
    return wh, load_file(wh, path)


def test_server_exposes_every_tool():
    async def names() -> list[str]:
        async with Client(tools_server.server) as client:
            return [t.name for t in (await client.list_tools()).tools]

    assert sorted(anyio.run(names)) == [
        "detect_patterns",
        "get_column_profile",
        "get_dbt_errors",
        "get_target_schema",
        "list_files",
        "profile_file",
        "run_dbt",
        "sample_rows",
        "test_transform",
    ]


def test_mcp_backend_returns_the_same_results_as_in_process(loaded):
    wh, file = loaded
    local = LocalTools(wh)
    with McpTools(tools_server.server, wh.run_id) as remote:
        assert remote.profile_file(file.file_id) == local.profile_file(file.file_id)
        assert remote.sample_rows(file.file_id, 2, "Tier = 'PRO'") == local.sample_rows(
            file.file_id, 2, "Tier = 'PRO'"
        )
        sql = "try_strptime(\"Joined\", ['%Y-%m-%d', '%m/%d/%Y'])::DATE"
        tested = remote.test_transform(file.file_id, sql, "date")
        assert (tested.converted, tested.non_null_input) == (2, 2)


def test_tool_errors_keep_their_message_over_mcp(loaded):
    wh, file = loaded
    with McpTools(tools_server.server, wh.run_id) as remote:
        with pytest.raises(ToolError, match=r"^Expected a single expression, got a DROP statement"):
            remote.test_transform(file.file_id, "DROP TABLE raw.export", "varchar")
        with pytest.raises(ToolError, match=r"^Unknown file_id 'nope'"):
            remote.sample_rows("nope", 1, None)
        with pytest.raises(ToolError, match="has not been rendered"):
            remote.run_dbt(None)


def test_unreachable_tools_server_is_a_tool_error():
    with (
        pytest.raises(ToolError, match="Cannot reach the tools server"),
        McpTools("http://127.0.0.1:9/mcp", "r", timeout_seconds=2),
    ):
        pass


def test_a_run_builds_with_every_data_tool_called_over_mcp(
    settings, data_dir, tmp_path, clean_rows, schemas, monkeypatch
):
    settings = settings.__class__(**{**settings.__dict__, "data_dir": data_dir})
    key = write_case(
        tmp_path / "case", "c", clean_rows["invoices"], schemas["invoices"], 3, ["duplicates"]
    )

    @contextmanager
    def over_mcp(_settings, wh):
        with McpTools(tools_server.server, wh.run_id) as tools:
            yield tools

    monkeypatch.setattr(agent.runner, "open_tools", over_mcp)
    session = session_factory(settings.database_url)()
    run = agent.runner.onboard(
        session, settings, FakeLLM(oracle_responder(key)), Tracer(None),
        customer_name="Acme Cloud", schema_name="invoices",
        paths=[tmp_path / "case" / "messy.csv"], review=answer_key_reviewer(key),
    )  # fmt: skip
    assert (run.status, run.error) == (RunStatus.DONE, None)
    assert run.build_report["row_accounting"][0]["balanced"]


def test_migrations_match_the_models(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    migrate(engine)
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []
