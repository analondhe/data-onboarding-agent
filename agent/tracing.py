"""MLflow tracing: one trace per run, one span per model call and tool call.

``Tracer(None)`` turns tracing off with the same interface, for tests that do not check traces.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

import mlflow

EXPERIMENT = "data-onboarding"


class _NullSpan:
    trace_id = None

    def set_inputs(self, inputs: Any) -> None:
        pass

    def set_outputs(self, outputs: Any) -> None:
        pass

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        pass


class Tracer:
    def __init__(self, tracking_uri: str | None, experiment: str = EXPERIMENT) -> None:
        self.enabled = bool(tracking_uri)
        self.trace_id: str | None = None
        if tracking_uri:
            if tracking_uri.startswith("sqlite:///"):
                Path(tracking_uri.removeprefix("sqlite:///")).parent.mkdir(
                    parents=True, exist_ok=True
                )
            logging.getLogger("mlflow").setLevel(logging.WARNING)
            logging.getLogger("alembic").setLevel(logging.WARNING)
            mlflow.set_tracking_uri(tracking_uri)
            mlflow.set_experiment(experiment)

    @contextmanager
    def span(
        self,
        name: str,
        span_type: str = "UNKNOWN",
        inputs: Any = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        """Open a span; the first span opened with no active parent starts the run's trace."""
        if not self.enabled:
            yield _NullSpan()
            return
        is_root = mlflow.get_current_active_span() is None
        with mlflow.start_span(name=name, span_type=span_type) as span:
            if inputs is not None:
                span.set_inputs(inputs)
            if attributes:
                span.set_attributes(attributes)
            if is_root:
                self.trace_id = span.trace_id
            yield span

    def tag_trace(self, tags: dict[str, str]) -> None:
        if self.enabled:
            mlflow.update_current_trace(tags=tags)

    def flush(self) -> None:
        if self.enabled and hasattr(mlflow, "flush_trace_async_logging"):
            mlflow.flush_trace_async_logging()

    @contextmanager
    def experiment_run(
        self, name: str, tags: dict[str, str], params: dict[str, Any]
    ) -> Iterator[None]:
        """An MLflow run (used for eval results); traces started inside link to it."""
        if not self.enabled:
            yield
            return
        with mlflow.start_run(run_name=name, tags=tags):
            mlflow.log_params(params)
            yield

    def log_metrics(self, metrics: dict[str, float]) -> None:
        if self.enabled:
            mlflow.log_metrics(metrics)
