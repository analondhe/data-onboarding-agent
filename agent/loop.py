"""The tool-calling loop: plain Python, no agent framework.

Send the conversation and tool definitions to the model, run the tools it calls, append the
results, and repeat until a submit tool accepts the model's answer or the step limit is hit.
Every problem the model can fix (bad JSON, unknown tool, invalid arguments, tool errors,
rejected submissions) goes back to it as a tool message; only model timeouts end the run.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from agent.llm import LLM, LLMTimeoutError, Message, ToolCall, complete_with_retries
from agent.tracing import Tracer
from tools.warehouse import ToolError

MAX_TOOL_RESULT_CHARS = 20_000
log = logging.getLogger(__name__)
NUDGE = "Respond with a tool call. When you are done, call {submit}."


@dataclass(frozen=True)
class SubmitOutcome:
    accepted: bool
    errors: list[str] = field(default_factory=list)
    value: Any = None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[Any], Any]  # submit tools return a SubmitOutcome
    submits: bool = False

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": inline_schema(self.args_model.model_json_schema()),
            },
        }


@dataclass
class LoopResult:
    status: str  # "submitted" or "failed"
    submission: Any = None
    reason: str | None = None
    steps: int = 0  # model calls that returned an answer
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    messages: list[Message] = field(default_factory=list)


def inline_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve $refs and drop titles: small models follow flat schemas more reliably."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].rsplit("/", 1)[-1]])
            return {k: walk(v) for k, v in node.items() if k not in ("title", "$defs")}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def _to_text(result: Any) -> str:
    if isinstance(result, str):
        text = result
    elif isinstance(result, BaseModel):
        text = result.model_dump_json()
    elif isinstance(result, list) and all(isinstance(r, BaseModel) for r in result):
        text = json.dumps([r.model_dump(mode="json") for r in result])
    else:
        text = json.dumps(result, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + " …[truncated]"
    return text


def _format_validation_error(exc: ValidationError) -> str:
    problems = []
    for error in exc.errors()[:10]:
        location = ".".join(str(part) for part in error["loc"]) or "arguments"
        problems.append(f"{location}: {error['msg']}")
    return "; ".join(problems)


def _execute(call: ToolCall, tools: dict[str, Tool]) -> tuple[str, SubmitOutcome | None]:
    """Run one tool call. Returns the text for the model and, for submit tools, the outcome."""
    tool = tools.get(call.name)
    if tool is None:
        return f"Error: unknown tool {call.name!r}. Available tools: {', '.join(tools)}", None
    try:
        raw = json.loads(call.arguments or "{}")
    except json.JSONDecodeError as exc:
        return f"Error: the arguments are not valid JSON ({exc}). Send a JSON object.", None
    if not isinstance(raw, dict):
        return "Error: the arguments must be a JSON object.", None
    try:
        args = tool.args_model.model_validate(raw)
    except ValidationError as exc:
        return f"Error: invalid arguments for {tool.name}: {_format_validation_error(exc)}", None
    try:
        result = tool.handler(args)
    except ToolError as exc:
        return f"Error: {exc}", None
    if not tool.submits:
        return _to_text(result), None
    outcome: SubmitOutcome = result
    if outcome.accepted:
        return "Accepted.", outcome
    problems = "\n".join(f"- {e}" for e in outcome.errors)
    return (
        f"Submission rejected. Fix these problems and call {tool.name} again:\n{problems}",
        outcome,
    )


def run_loop(
    llm: LLM,
    system_prompt: str,
    user_prompt: str,
    tools: list[Tool],
    *,
    max_steps: int,
    tracer: Tracer,
    retries: int = 3,
    backoff_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> LoopResult:
    by_name = {tool.name: tool for tool in tools}
    definitions = [tool.definition() for tool in tools]
    submit_names = " or ".join(t.name for t in tools if t.submits)
    messages: list[Message] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    result = LoopResult(status="failed", messages=messages)

    for _ in range(max_steps):
        with tracer.span("llm", "CHAT_MODEL", attributes={"model": llm.model}) as span:
            started = time.monotonic()
            try:
                response = complete_with_retries(
                    llm, messages, definitions, retries=retries,
                    backoff_seconds=backoff_seconds, sleep=sleep,
                )  # fmt: skip
            except LLMTimeoutError as exc:
                span.set_attributes({"error": str(exc)})
                result.reason = f"The model did not respond after {retries + 1} attempts: {exc}"
                return result
            latency_ms = round((time.monotonic() - started) * 1000)
            span.set_attributes(
                {
                    "prompt_tokens": response.prompt_tokens,
                    "completion_tokens": response.completion_tokens,
                    "latency_ms": latency_ms,
                    "tool_calls": [call.name for call in response.tool_calls],
                }
            )
        result.steps += 1
        result.prompt_tokens += response.prompt_tokens
        result.completion_tokens += response.completion_tokens
        messages.append(response.as_message())
        log.info(
            "step %d: %s (%d+%d tokens, %.1fs)",
            result.steps,
            ", ".join(call.name for call in response.tool_calls) or "no tool call",
            response.prompt_tokens,
            response.completion_tokens,
            latency_ms / 1000,
        )

        if not response.tool_calls:
            messages.append({"role": "user", "content": NUDGE.format(submit=submit_names)})
            continue

        for call in response.tool_calls:
            result.tool_calls += 1
            with tracer.span(f"tool:{call.name}", "TOOL", inputs=call.arguments) as span:
                text, outcome = _execute(call, by_name)
                span.set_outputs(text)
                if text.startswith(("Error:", "Submission rejected")):
                    span.set_attributes({"error": True})
                    log.info("  %s -> %s", call.name, text[:300].replace("\n", " "))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
            if outcome is not None and outcome.accepted:
                result.status = "submitted"
                result.submission = outcome.value
                return result

    result.reason = f"Reached the step limit ({max_steps}) without an accepted {submit_names}"
    return result
