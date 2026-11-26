from __future__ import annotations

import pytest
from pydantic import BaseModel

from agent.llm import (
    FakeError,
    FakeLLM,
    FakeReply,
    FakeTimeout,
    LLMUnavailableError,
    complete_with_retries,
)
from agent.loop import SubmitOutcome, Tool, inline_schema, run_loop
from agent.tracing import Tracer
from tools.warehouse import ToolError


class EchoArgs(BaseModel):
    text: str


class AnswerArgs(BaseModel):
    value: int


def make_tools() -> list[Tool]:
    def echo(args: EchoArgs) -> dict:
        if args.text == "boom":
            raise ToolError("echo cannot say boom")
        return {"echo": args.text}

    def submit(args: AnswerArgs) -> SubmitOutcome:
        if args.value != 42:
            return SubmitOutcome(accepted=False, errors=[f"{args.value} is not the answer"])
        return SubmitOutcome(accepted=True, value=args.value)

    return [
        Tool("echo", "Echo text back", EchoArgs, echo),
        Tool("submit_answer", "Submit the answer", AnswerArgs, submit, submits=True),
    ]


def run(script, max_steps: int = 10, retries: int = 3):
    llm = FakeLLM(script)
    sleeps: list[float] = []
    result = run_loop(
        llm,
        "system",
        "user",
        make_tools(),
        max_steps=max_steps,
        tracer=Tracer(None),
        retries=retries,
        backoff_seconds=1.0,
        sleep=sleeps.append,
    )
    return result, llm, sleeps


def last_tool_message(llm: FakeLLM) -> str:
    return next(m["content"] for m in reversed(llm.calls[-1]) if m["role"] == "tool")


def test_runs_tools_and_stops_at_an_accepted_submission():
    result, llm, _ = run(
        [
            FakeReply(tool_calls=[("echo", {"text": "hi"})]),
            FakeReply(tool_calls=[("submit_answer", {"value": 42})]),
        ]
    )
    assert (result.status, result.submission, result.steps, result.tool_calls) == (
        "submitted",
        42,
        2,
        2,
    )
    assert last_tool_message(llm) == '{"echo": "hi"}'
    assert result.messages[-1] == {
        "role": "tool",
        "tool_call_id": "call_2_0",
        "content": "Accepted.",
    }


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (("echo", "{not json"), "Error: the arguments are not valid JSON"),
        (("echo", "[1, 2]"), "Error: the arguments must be a JSON object"),
        (("nope", {}), "Error: unknown tool 'nope'. Available tools: echo, submit_answer"),
        (("echo", {"txt": "hi"}), "Error: invalid arguments for echo: text: Field required"),
        (("echo", {"text": "boom"}), "Error: echo cannot say boom"),
        (("submit_answer", {"value": 7}), "Submission rejected. Fix these problems"),
    ],
)
def test_fixable_problems_go_back_to_the_model(call, expected):
    result, llm, _ = run(
        [FakeReply(tool_calls=[call]), FakeReply(tool_calls=[("submit_answer", {"value": 42})])]
    )
    assert result.status == "submitted"
    assert last_tool_message(llm).startswith(expected)


def test_reply_without_tool_call_is_nudged():
    result, llm, _ = run(
        [FakeReply(content="thinking..."), FakeReply(tool_calls=[("submit_answer", {"value": 42})])]
    )
    assert result.status == "submitted"
    assert llm.calls[1][-1] == {
        "role": "user",
        "content": "Respond with a tool call. When you are done, call submit_answer.",
    }


def test_timeouts_are_retried_with_backoff():
    result, _, sleeps = run(
        [FakeTimeout(), FakeTimeout(), FakeReply(tool_calls=[("submit_answer", {"value": 42})])]
    )
    assert result.status == "submitted"
    assert sleeps == [1.0, 2.0]


def test_persistent_timeouts_end_the_run_cleanly():
    result, llm, sleeps = run([FakeTimeout()] * 4, retries=3)
    assert result.status == "failed"
    assert result.reason.startswith("The model did not respond after 4 attempts")
    assert len(llm.calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_step_limit_ends_the_run_cleanly():
    result, llm, _ = run(lambda messages: FakeReply(tool_calls=[("echo", {"text": "x"})]), 3)
    assert result.status == "failed"
    assert result.reason == "Reached the step limit (3) without an accepted submit_answer"
    assert len(llm.calls) == 3


def test_complete_with_retries_reraises_after_the_last_attempt():
    with pytest.raises(LLMUnavailableError):
        complete_with_retries(FakeLLM([FakeTimeout()] * 2), [], [], retries=1, sleep=lambda s: None)


def test_permanent_errors_are_not_retried():
    result, llm, sleeps = run([FakeError("AuthenticationError: bad key")])
    assert (result.status, result.reason) == (
        "failed",
        "The model call failed: AuthenticationError: bad key",
    )
    assert (len(llm.calls), sleeps) == (1, [])


def test_tool_definitions_are_flat_json_schemas():
    class Inner(BaseModel):
        a: int

    class Outer(BaseModel):
        items: list[Inner]

    schema = inline_schema(Outer.model_json_schema())
    assert "$defs" not in schema
    assert schema["properties"]["items"]["items"] == {
        "properties": {"a": {"type": "integer"}},
        "required": ["a"],
        "type": "object",
    }
