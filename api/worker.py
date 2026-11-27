"""Background worker inside the API process: advances runs off the request thread.

Each job opens its own database session and advances one run (profile, map, or build). A run
is never advanced twice at once. On startup, runs left mid-phase by a crash are marked failed
so they can be resumed with POST /runs/{id}/start.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from sqlalchemy.orm import Session, sessionmaker

from agent.llm import LLM
from agent.runner import advance
from agent.settings import Settings
from agent.tracing import Tracer
from api.logs import run_context
from api.models import Run, RunStatus

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        settings: Settings,
        sessions: sessionmaker[Session],
        make_llm: Callable[[], LLM],
        *,
        max_workers: int = 2,
        inline: bool = False,  # run jobs on the calling thread (tests)
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.make_llm = make_llm
        self.inline = inline
        self._executor = None if inline else ThreadPoolExecutor(max_workers, "run-worker")
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def submit(self, run_id: str) -> bool:
        """Start advancing a run; False if it is already being advanced."""
        with self._lock:
            if run_id in self._active:
                return False
            self._active.add(run_id)
        if self._executor is None:
            self._job(run_id)
        else:
            future: Future[None] = self._executor.submit(self._job, run_id)
            future.add_done_callback(lambda f: f.exception())  # errors are logged in _job
        return True

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._active

    def _job(self, run_id: str) -> None:
        with run_context(run_id), self.sessions() as session:
            try:
                run = session.get(Run, run_id)
                if run is None:
                    return
                log.info("advancing run from %s", run.status)
                try:
                    llm = self.make_llm()
                except ValueError as exc:  # e.g. LLM_MODEL not set
                    run.status, run.error = RunStatus.FAILED, str(exc)
                    session.commit()
                    return
                advance(session, self.settings, llm, Tracer(self.settings.mlflow_tracking_uri), run)
                log.info("run is %s%s", run.status, f": {run.error}" if run.error else "")
            except Exception as exc:  # never leave a run stuck mid-phase
                log.exception("run crashed")
                session.rollback()
                run = session.get(Run, run_id)
                if run is not None:
                    run.status, run.error = RunStatus.FAILED, f"Internal error: {exc}"
                    session.commit()
            finally:
                with self._lock:
                    self._active.discard(run_id)

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
