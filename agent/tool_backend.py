"""How the agent reaches the data tools: in-process (CLI, tests) or over MCP (Compose).

Both backends are bound to one run and return the same Pydantic models, so the phases do not
know which one they use. ``open_tools`` picks MCP when ``TOOLS_URL`` is set.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from anyio.from_thread import start_blocking_portal
from pydantic import BaseModel, TypeAdapter

from agent.settings import Settings
from tools import dbt_runner, profiling, transforms
from tools.dbt_runner import DbtRunResult, FailedNode
from tools.profiling import ColumnProfile, FileProfile, SampleRows
from tools.transforms import TransformTest
from tools.warehouse import ToolError, Warehouse


class RunTools(Protocol):
    run_id: str

    def profile_file(self, file_id: str) -> FileProfile: ...
    def get_column_profile(self, file_id: str, column: str) -> ColumnProfile: ...
    def sample_rows(self, file_id: str, n: int, where: str | None) -> SampleRows: ...
    def test_transform(
        self, file_id: str, sql_expression: str, target_type: str
    ) -> TransformTest: ...
    def run_dbt(self, select: str | None) -> DbtRunResult: ...
    def get_dbt_errors(self) -> list[FailedNode]: ...


class LocalTools:
    """Calls the tool functions directly on the run's warehouse."""

    def __init__(self, wh: Warehouse) -> None:
        self.wh = wh
        self.run_id = wh.run_id

    def profile_file(self, file_id: str) -> FileProfile:
        return profiling.profile_file(self.wh, file_id)

    def get_column_profile(self, file_id: str, column: str) -> ColumnProfile:
        return profiling.get_column_profile(self.wh, file_id, column)

    def sample_rows(self, file_id: str, n: int, where: str | None) -> SampleRows:
        return profiling.sample_rows(self.wh, file_id, n, where)

    def test_transform(self, file_id: str, sql_expression: str, target_type: str) -> TransformTest:
        return transforms.test_transform(self.wh, file_id, sql_expression, target_type)

    def run_dbt(self, select: str | None) -> DbtRunResult:
        return dbt_runner.run_dbt(self.wh, select)

    def get_dbt_errors(self) -> list[FailedNode]:
        return dbt_runner.get_dbt_errors(self.wh)


_TOOL_ERROR_PREFIX = re.compile(r"^Error executing tool \w+: ")
_FAILED_NODES = TypeAdapter(list[FailedNode])


class McpTools:
    """Calls the tools server over MCP. ``target`` is a URL, or an MCPServer for tests."""

    def __init__(self, target: Any, run_id: str, timeout_seconds: float = 900) -> None:
        self.target = target
        self.run_id = run_id
        self.timeout_seconds = timeout_seconds

    def __enter__(self) -> McpTools:
        from mcp.client import Client

        self._portal_cm = start_blocking_portal()
        self._portal = self._portal_cm.__enter__()
        # The client's task groups must enter and exit in one task; the portal keeps it there.
        self._client_cm = self._portal.wrap_async_context_manager(
            Client(self.target, read_timeout_seconds=self.timeout_seconds)
        )
        try:
            self._client = self._client_cm.__enter__()
        except Exception as exc:
            self._portal_cm.__exit__(None, None, None)
            raise ToolError(f"Cannot reach the tools server at {self.target}: {exc}") from exc
        return self

    def __exit__(self, *exc_info: object) -> None:
        try:
            self._client_cm.__exit__(None, None, None)
        finally:
            self._portal_cm.__exit__(None, None, None)

    def _call(self, name: str, **arguments: Any) -> Any:
        result = self._portal.call(
            self._client.call_tool, name, {"run_id": self.run_id, **arguments}
        )
        text = " ".join(getattr(block, "text", "") for block in result.content)
        if result.is_error:
            raise ToolError(_TOOL_ERROR_PREFIX.sub("", text) or f"{name} failed")
        data = result.structured_content
        # Tools returning lists come back wrapped as {"result": [...]}
        if isinstance(data, dict) and set(data) == {"result"}:
            return data["result"]
        return data

    def _model(self, model: type[BaseModel], name: str, **arguments: Any) -> Any:
        return model.model_validate(self._call(name, **arguments))

    def profile_file(self, file_id: str) -> FileProfile:
        return self._model(FileProfile, "profile_file", file_id=file_id)

    def get_column_profile(self, file_id: str, column: str) -> ColumnProfile:
        return self._model(ColumnProfile, "get_column_profile", file_id=file_id, column=column)

    def sample_rows(self, file_id: str, n: int, where: str | None) -> SampleRows:
        return self._model(SampleRows, "sample_rows", file_id=file_id, n=n, where=where)

    def test_transform(self, file_id: str, sql_expression: str, target_type: str) -> TransformTest:
        return self._model(
            TransformTest,
            "test_transform",
            file_id=file_id,
            sql_expression=sql_expression,
            target_type=target_type,
        )

    def run_dbt(self, select: str | None) -> DbtRunResult:
        return self._model(DbtRunResult, "run_dbt", select=select)

    def get_dbt_errors(self) -> list[FailedNode]:
        return _FAILED_NODES.validate_python(self._call("get_dbt_errors"))


@contextmanager
def open_tools(settings: Settings, wh: Warehouse) -> Iterator[RunTools]:
    """The run's tools: over MCP when ``TOOLS_URL`` is set, in-process otherwise."""
    if settings.tools_url:
        with McpTools(settings.tools_url, wh.run_id) as tools:
            yield tools
    else:
        yield LocalTools(wh)
