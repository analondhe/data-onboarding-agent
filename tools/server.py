"""MCP server exposing the profiling, schema, transform, and dbt tools over streamable HTTP.

Every tool takes ``run_id`` and works on that run's warehouse under ``DATA_DIR``. Problems the
caller can fix (unknown file, unsafe SQL, ...) come back as tool errors with a readable message.

  python -m tools.server            # listens on 0.0.0.0:8100, MCP endpoint /mcp, GET /health
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from typing import Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as McpToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from datagen.schemas import load_schema
from tools import dbt_runner, profiling, transforms
from tools.dbt_runner import DbtRunResult, FailedNode
from tools.profiling import ColumnPatterns, ColumnProfile, FileProfile, SampleRows
from tools.transforms import TransformTest
from tools.warehouse import FileInfo, ToolError, Warehouse

server = MCPServer(
    "data-onboarding-tools",
    instructions="Deterministic data tools over each run's DuckDB warehouse.",
)
F = TypeVar("F", bound=Callable[..., Any])


def tool(description: str) -> Callable[[F], F]:
    """Register a tool. Our ToolError becomes the SDK's, so its message reaches the caller;
    any other exception stays a generic server error."""

    def register(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except ToolError as exc:
                raise McpToolError(str(exc)) from exc

        server.tool(description=description)(wrapper)
        return func

    return register


server = MCPServer(
    "data-onboarding-tools",
    instructions="Deterministic data tools over each run's DuckDB warehouse.",
)


def _wh(run_id: str) -> Warehouse:
    return Warehouse.for_run(run_id)


@tool("Files loaded into the run: ids, names, row counts")
def list_files(run_id: str) -> list[FileInfo]:
    return profiling.list_files(_wh(run_id))


@tool("Profile every column of a file and store the profiles")
def profile_file(run_id: str, file_id: str) -> FileProfile:
    return profiling.profile_file(_wh(run_id), file_id)


@tool("The stored profile of one source column")
def get_column_profile(run_id: str, file_id: str, column: str) -> ColumnProfile:
    return profiling.get_column_profile(_wh(run_id), file_id, column)


@tool("Up to 20 raw rows, optionally filtered by a SQL condition over one row")
def sample_rows(run_id: str, file_id: str, n: int = 10, where: str | None = None) -> SampleRows:
    return profiling.sample_rows(_wh(run_id), file_id, n, where)


@tool("Value shape counts for one column, e.g. YYYY-MM-DD vs MM/DD/YYYY")
def detect_patterns(run_id: str, file_id: str, column: str) -> ColumnPatterns:
    return profiling.detect_patterns(_wh(run_id), file_id, column)


@tool("Share of rows a single-row SQL expression converts to the target type")
def test_transform(
    run_id: str, file_id: str, sql_expression: str, target_type: str
) -> TransformTest:
    return transforms.test_transform(_wh(run_id), file_id, sql_expression, target_type)


@tool("A target schema as JSON: columns, types, required, allowed values")
def get_target_schema(schema_name: str) -> dict[str, Any]:
    try:
        return load_schema(schema_name)
    except FileNotFoundError as exc:
        raise ToolError(str(exc)) from exc


@tool("Run dbt build on the run's rendered project; pass and fail counts")
def run_dbt(run_id: str, select: str | None = None) -> DbtRunResult:
    return dbt_runner.run_dbt(_wh(run_id), select)


@tool("Error text and up to 5 failing rows per failed dbt model or test")
def get_dbt_errors(run_id: str) -> list[FailedNode]:
    return dbt_runner.get_dbt_errors(_wh(run_id))


@server.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "tools": len(await server.list_tools())})


def app() -> Any:
    """The ASGI app. Host checks are relaxed because Compose reaches it as ``tools:8100``."""
    return server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


def main() -> None:
    import uvicorn

    uvicorn.run(
        app(),
        host=os.environ.get("TOOLS_HOST", "0.0.0.0"),
        port=int(os.environ.get("TOOLS_PORT", "8100")),
    )


if __name__ == "__main__":
    main()
