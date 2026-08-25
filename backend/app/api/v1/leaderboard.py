"""Strict completed-run leaderboard and Dashboard metrics."""

from fastapi import APIRouter, Query
from sqlalchemy import asc, desc, func, select

from app.api.deps import PaginationDep, SessionDep
from app.core.constants import PROTOCOL_VERSION
from app.models import Benchmark, EvaluationRun, Model, RunStatus
from app.schemas.metrics import DashboardSummary, LeaderboardEntry, LeaderboardList

router = APIRouter(tags=["metrics"])


def _entry(run: EvaluationRun, model: Model, benchmark: Benchmark) -> LeaderboardEntry:
    snapshot = dict(run.model_parameters_snapshot or {})
    model_snapshot = dict(snapshot.get("model", {}))
    benchmark_snapshot = dict(snapshot.get("benchmark", {}))
    return LeaderboardEntry(
        run_id=run.id,
        model_id=model.id,
        model_name=str(model_snapshot.get("name") or model.name),
        benchmark_id=benchmark.id,
        benchmark_slug=str(benchmark_snapshot.get("slug") or benchmark.slug),
        benchmark_name=str(benchmark_snapshot.get("name") or benchmark.name),
        benchmark_version=str(benchmark_snapshot.get("version") or benchmark.version),
        benchmark_hash=run.benchmark_hash_snapshot,
        is_demo=bool(benchmark_snapshot.get("is_demo", benchmark.is_demo)),
        protocol_version=run.protocol_version,
        score=float(run.score or 0),
        answered_accuracy=(
            float(run.answered_accuracy) if run.answered_accuracy is not None else None
        ),
        completion_rate=float(run.completion_rate or 0),
        average_latency_ms=run.average_latency_ms,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        estimated_cost=(float(run.estimated_cost) if run.estimated_cost is not None else None),
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _leaderboard_rows(
    session: SessionDep,
    *,
    model_id: str | None = None,
    benchmark_id: str | None = None,
    protocol_version: str = PROTOCOL_VERSION,
    order: str = "score_desc",
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[LeaderboardEntry], int]:
    filters = [
        EvaluationRun.status == RunStatus.COMPLETED,
        EvaluationRun.protocol_version == protocol_version,
    ]
    if model_id:
        filters.append(EvaluationRun.model_id == model_id)
    if benchmark_id:
        filters.append(EvaluationRun.benchmark_id == benchmark_id)
    order_by = {
        "score_desc": desc(EvaluationRun.score),
        "score_asc": asc(EvaluationRun.score),
        "latency_asc": asc(EvaluationRun.average_latency_ms),
        "newest": desc(EvaluationRun.finished_at),
    }[order]
    total = session.scalar(select(func.count()).select_from(EvaluationRun).where(*filters)) or 0
    rows = session.execute(
        select(EvaluationRun, Model, Benchmark)
        .join(Model, Model.id == EvaluationRun.model_id)
        .join(Benchmark, Benchmark.id == EvaluationRun.benchmark_id)
        .where(*filters)
        .order_by(order_by, EvaluationRun.finished_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [_entry(*row) for row in rows], total


@router.get("/leaderboard", response_model=LeaderboardList, summary="严格总分排行榜")
def leaderboard(
    session: SessionDep,
    pagination: PaginationDep,
    model_id: str | None = None,
    benchmark_id: str | None = None,
    protocol_version: str = PROTOCOL_VERSION,
    order: str = Query(default="score_desc", pattern="^(score_desc|score_asc|latency_asc|newest)$"),
) -> LeaderboardList:
    items, total = _leaderboard_rows(
        session,
        model_id=model_id,
        benchmark_id=benchmark_id,
        protocol_version=protocol_version,
        order=order,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return LeaderboardList(
        items=items, total=total, offset=pagination.offset, limit=pagination.limit
    )


@router.get("/metrics/summary", response_model=DashboardSummary, summary="Dashboard 汇总")
def summary(session: SessionDep) -> DashboardSummary:
    model_count = session.scalar(select(func.count()).select_from(Model)) or 0
    benchmark_count = session.scalar(select(func.count()).select_from(Benchmark)) or 0
    run_count = session.scalar(select(func.count()).select_from(EvaluationRun)) or 0
    completed = (
        session.scalar(
            select(func.count())
            .select_from(EvaluationRun)
            .where(
                EvaluationRun.status == RunStatus.COMPLETED,
                EvaluationRun.protocol_version == PROTOCOL_VERSION,
            )
        )
        or 0
    )
    failed = (
        session.scalar(
            select(func.count())
            .select_from(EvaluationRun)
            .where(
                EvaluationRun.status == RunStatus.FAILED,
                EvaluationRun.protocol_version == PROTOCOL_VERSION,
            )
        )
        or 0
    )
    aggregate = session.execute(
        select(
            func.avg(EvaluationRun.score),
            func.avg(EvaluationRun.average_latency_ms),
            func.coalesce(func.sum(EvaluationRun.input_tokens), 0),
            func.count(EvaluationRun.input_tokens),
            func.coalesce(func.sum(EvaluationRun.output_tokens), 0),
            func.count(EvaluationRun.output_tokens),
            func.coalesce(func.sum(EvaluationRun.estimated_cost), 0),
            func.count(EvaluationRun.estimated_cost),
        ).where(
            EvaluationRun.status == RunStatus.COMPLETED,
            EvaluationRun.protocol_version == PROTOCOL_VERSION,
        )
    ).one()
    recent, _ = _leaderboard_rows(session, order="newest", limit=5)
    (
        average_score,
        average_latency,
        input_tokens,
        input_reports,
        output_tokens,
        output_reports,
        cost,
        cost_reports,
    ) = aggregate
    return DashboardSummary(
        model_count=model_count,
        benchmark_count=benchmark_count,
        run_count=run_count,
        completed_run_count=completed,
        failed_run_count=failed,
        average_score=float(average_score) if average_score is not None else None,
        average_latency_ms=float(average_latency) if average_latency is not None else None,
        total_input_tokens=(
            int(input_tokens or 0) if not completed or int(input_reports) == completed else None
        ),
        total_output_tokens=(
            int(output_tokens or 0) if not completed or int(output_reports) == completed else None
        ),
        total_estimated_cost=(
            float(cost or 0) if not completed or int(cost_reports) == completed else None
        ),
        recent_runs=recent,
    )
