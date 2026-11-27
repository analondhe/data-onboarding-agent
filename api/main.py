"""HTTP API: create runs, upload files, start the agent, review mappings, read the report.

uvicorn --factory api.main:create_app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from agent.llm import LLM, OpenAICompatibleLLM
from agent.review import apply_review, finish_review
from agent.runner import ACTIVE, add_file, create_run, recover_interrupted
from agent.settings import Settings
from api.db import make_engine, migrate
from api.health import check_database, check_model, check_tools
from api.logs import configure_logging
from api.models import Run, RunStatus
from api.schemas import (
    CreateRun,
    DefectOut,
    MappingOut,
    ReportOut,
    ReviewIn,
    ReviewOut,
    RunOut,
)
from api.uploads import save_upload
from api.worker import Worker
from datagen.schemas import TABLES, load_schema
from tools.warehouse import ToolError

log = logging.getLogger(__name__)


def _db(request: Request) -> Iterator[Session]:
    with request.app.state.sessions() as session:
        yield session


DB = Annotated[Session, Depends(_db)]


def create_app(
    settings: Settings | None = None,
    make_llm: Callable[[], LLM] | None = None,
    *,
    inline_worker: bool = False,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if make_llm is None:

        def make_llm() -> LLM:
            return OpenAICompatibleLLM.from_settings(settings)

    engine: Engine = make_engine(settings.database_url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    worker = Worker(settings, sessions, make_llm, inline=inline_worker)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not inline_worker:
            configure_logging()
        migrate(engine)
        with sessions() as session:
            if interrupted := recover_interrupted(session):
                log.warning("marked interrupted runs as failed: %s", ", ".join(interrupted))
        yield
        worker.shutdown()

    app = FastAPI(title="Data Onboarding Agent", version="0.6.0", lifespan=lifespan)
    # The web app (another origin, :3000) calls the API straight from the browser.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    def out(run: Run) -> RunOut:
        return RunOut.of(run, busy=worker.is_active(run.id))

    def get_run(session: Session, run_id: str) -> Run:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Run {run_id} not found")
        return run

    def conflict(message: str) -> HTTPException:
        return HTTPException(status.HTTP_409_CONFLICT, message)

    @app.get("/health")
    def health(response: Response) -> dict[str, Any]:
        checks = {
            "database": check_database(engine),
            "model": check_model(settings),
            "tools": check_tools(settings),
        }
        ok = all(value.startswith("ok") for value in checks.values())
        response.status_code = status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "ok" if ok else "degraded", "checks": checks}

    @app.get("/schemas")
    def schemas() -> list[dict[str, Any]]:
        return [load_schema(name) for name in TABLES]

    @app.get("/runs")
    def list_runs(session: DB, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[RunOut]:
        """Most recent runs first."""
        runs = session.scalars(select(Run).order_by(Run.created_at.desc()).limit(limit))
        return [out(run) for run in runs]

    @app.post("/runs", status_code=status.HTTP_201_CREATED)
    def create(body: CreateRun, session: DB) -> RunOut:
        try:
            run = create_run(
                session, customer_name=body.customer_name, schema_name=body.target_schema
            )
        except FileNotFoundError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Unknown target schema {body.target_schema!r}; use one of: {', '.join(TABLES)}",
            ) from exc
        return out(run)

    @app.post("/runs/{run_id}/files", status_code=status.HTTP_201_CREATED)
    async def upload(
        run_id: str, session: DB, files: Annotated[list[UploadFile], File()]
    ) -> RunOut:
        run = get_run(session, run_id)
        if run.status not in (RunStatus.CREATED, RunStatus.FAILED) or run.mappings:
            raise conflict(f"Run {run_id} is {run.status}; files can only be added before mapping")
        uploads_dir = settings.data_dir / "runs" / run_id / "uploads"
        for upload in files:
            path = await save_upload(upload, uploads_dir, settings.max_upload_mb)
            try:
                add_file(session, settings, run, path)
            except ToolError as exc:
                path.unlink(missing_ok=True)
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        return out(run)

    @app.post("/runs/{run_id}/start", status_code=status.HTTP_202_ACCEPTED)
    def start(run_id: str, session: DB) -> RunOut:
        """Profile and map the files, or resume a failed run from its last completed phase."""
        run = get_run(session, run_id)
        if not run.files:
            raise conflict(f"Run {run_id} has no files; upload at least one first")
        if run.status in ACTIVE or worker.is_active(run_id):
            raise conflict(f"Run {run_id} is already {run.status}")
        if run.status == RunStatus.AWAITING_REVIEW:
            raise conflict(f"Run {run_id} is waiting for review; POST /runs/{run_id}/review")
        if run.status == RunStatus.DONE:
            raise conflict(f"Run {run_id} is done")
        worker.submit(run_id)
        session.refresh(run)
        return out(run)

    @app.get("/runs/{run_id}")
    def get(run_id: str, session: DB) -> RunOut:
        return out(get_run(session, run_id))

    @app.get("/runs/{run_id}/mappings")
    def mappings(run_id: str, session: DB) -> list[MappingOut]:
        run = get_run(session, run_id)
        samples = {(f.id, p.column_name): p.sample_values for f in run.files for p in f.profiles}
        return [MappingOut.of(m, samples) for m in run.mappings]

    @app.get("/runs/{run_id}/defects")
    def defects(run_id: str, session: DB) -> list[DefectOut]:
        run = get_run(session, run_id)
        return [DefectOut.model_validate(d, from_attributes=True) for d in run.defects]

    @app.post("/runs/{run_id}/review")
    def review(run_id: str, body: ReviewIn, session: DB, response: Response) -> ReviewOut:
        """Apply approve/edit/reject decisions; once nothing is pending, start the build."""
        run = get_run(session, run_id)
        if run.status not in (RunStatus.AWAITING_REVIEW, RunStatus.FAILED) or not run.mappings:
            raise conflict(f"Run {run_id} is {run.status}; there is nothing to review")
        if worker.is_active(run_id):
            raise conflict(f"Run {run_id} is already being processed")
        try:
            apply_review(session, run, body.decisions, actor="api")
        except ToolError as exc:
            session.rollback()
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        pending = finish_review(session, run)
        building = pending == 0 and body.build
        if building:
            worker.submit(run_id)
            response.status_code = status.HTTP_202_ACCEPTED
        session.refresh(run)
        return ReviewOut(pending_reviews=pending, building=building, run=out(run))

    @app.get("/runs/{run_id}/report")
    def report(run_id: str, session: DB) -> ReportOut:
        run = get_run(session, run_id)
        trace_url = None
        if settings.mlflow_ui_url and run.trace_id and run.trace_experiment_id:
            trace_url = (
                f"{settings.mlflow_ui_url.rstrip('/')}/#/experiments/{run.trace_experiment_id}"
                f"/traces?selectedEvaluationId={run.trace_id}"
            )
        return ReportOut(
            run=out(run),
            duration_seconds=run.duration_seconds,
            model_calls=run.model_calls,
            tool_calls=run.tool_calls,
            prompt_tokens=run.prompt_tokens,
            completion_tokens=run.completion_tokens,
            trace_id=run.trace_id,
            trace_url=trace_url,
            build=_with_filenames(run),
        )

    app.state.worker = worker
    app.state.sessions = sessions
    return app


def _with_filenames(run: Run) -> dict[str, Any] | None:
    """The stored build report, with each row-accounting entry's uploaded file name."""
    if run.build_report is None:
        return None
    names = {f.warehouse_file_id: f.filename for f in run.files}
    accounting = [
        {**a, "filename": names.get(a["file_id"])} for a in run.build_report["row_accounting"]
    ]
    return {**run.build_report, "row_accounting": accounting}
