"""Model clients: an OpenAI-compatible client and a scripted FakeLLM with the same interface.

Both return an ``LLMResponse``. Tool-call arguments are kept as the raw JSON string the model
produced; the loop parses them, so malformed JSON is reported back to the model, not raised.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import openai

from agent.settings import Settings

Message = dict[str, Any]


class LLMError(Exception):
    """The model call failed and retrying will not help (bad key, unknown model, no credit)."""


class LLMUnavailableError(LLMError):
    """The model did not answer: timeout, connection error, rate limit, or server error."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text from the model


@dataclass(frozen=True)
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def as_message(self) -> Message:
        """The assistant message to append to the conversation."""
        message: Message = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message


class LLM(Protocol):
    model: str

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> LLMResponse:
        """One model call. Raises LLMUnavailableError when retrying may help, LLMError if not."""
        ...


class OpenAICompatibleLLM:
    """Any OpenAI-compatible chat endpoint (Ollama by default)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout_seconds: float = 300,
        reasoning_effort: str | None = None,
    ) -> None:
        if not model:
            raise ValueError("No model configured: set LLM_MODEL (for example qwen3:8b)")
        self.model = model
        self._reasoning_effort = reasoning_effort
        self._client = openai.OpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout_seconds, max_retries=0
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAICompatibleLLM:
        return cls(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            timeout_seconds=settings.llm_timeout_seconds,
            reasoning_effort=settings.llm_reasoning_effort,
        )

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> LLMResponse:
        extra: dict[str, Any] = {}
        if self._reasoning_effort:
            extra["reasoning_effort"] = self._reasoning_effort
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                tools=tools,  # type: ignore[arg-type]
                temperature=0,
                **extra,
            )
        except (
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.RateLimitError,
            openai.InternalServerError,
        ) as exc:
            raise LLMUnavailableError(f"{type(exc).__name__}: {exc}") from exc
        except openai.APIError as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        if not response.choices:
            raise LLMUnavailableError(f"empty response from {self.model}")
        message = response.choices[0].message
        usage = response.usage
        return LLMResponse(
            content=message.content,
            tool_calls=[
                ToolCall(call.id, call.function.name, call.function.arguments or "")
                for call in message.tool_calls or []
                if call.type == "function"
            ],
            model=response.model or self.model,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )


def complete_with_retries(
    llm: LLM,
    messages: list[Message],
    tools: list[dict[str, Any]],
    *,
    retries: int = 3,
    backoff_seconds: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> LLMResponse:
    """Call the model, retrying unavailability ``retries`` times with exponential backoff."""
    for attempt in range(retries + 1):
        try:
            return llm.complete(messages, tools)
        except LLMUnavailableError:
            if attempt == retries:
                raise
            sleep(backoff_seconds * 2**attempt)
    raise AssertionError("unreachable")


# --- Fake model for tests --------------------------------------------------------------------


@dataclass(frozen=True)
class FakeTimeout:
    """A scripted step that times out instead of answering."""


@dataclass(frozen=True)
class FakeError:
    """A scripted step that fails permanently, like a rejected API key."""

    message: str = "scripted error"


@dataclass(frozen=True)
class FakeReply:
    """A scripted answer. Each tool call is (name, arguments); a str argument is sent as is,
    so it can be malformed JSON."""

    tool_calls: list[tuple[str, dict[str, Any] | str]] = field(default_factory=list)
    content: str | None = None


FakeStep = FakeReply | FakeTimeout | FakeError
Responder = Callable[[list[Message]], FakeStep]


class FakeLLM:
    """Replays scripted steps, or asks a responder function, in place of a real model."""

    def __init__(self, script: Iterable[FakeStep] | Responder, model: str = "fake-llm") -> None:
        self.model = model
        self.calls: list[list[Message]] = []
        self._responder: Responder | None = script if callable(script) else None
        self._script: Iterator[FakeStep] = iter(()) if callable(script) else iter(script)

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        if self._responder is not None:
            step = self._responder(messages)
        else:
            try:
                step = next(self._script)
            except StopIteration:
                raise AssertionError("FakeLLM script ran out of steps") from None
        if isinstance(step, FakeTimeout):
            raise LLMUnavailableError("scripted timeout")
        if isinstance(step, FakeError):
            raise LLMError(step.message)
        n = len(self.calls)
        return LLMResponse(
            content=step.content,
            tool_calls=[
                ToolCall(
                    id=f"call_{n}_{i}",
                    name=name,
                    arguments=args if isinstance(args, str) else json.dumps(args),
                )
                for i, (name, args) in enumerate(step.tool_calls)
            ],
            model=self.model,
            prompt_tokens=sum(len(str(m.get("content", ""))) for m in messages) // 4,
            completion_tokens=10,
        )
