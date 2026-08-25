"""Dashboard and leaderboard projection schemas."""

from datetime import datetime

from app.schemas.base import ORMModel


class LeaderboardEntry(ORMModel):
    run_id: str
    model_id: str
    model_name: str
    benchmark_id: str
    benchmark_slug: str
    benchmark_name: str
    benchmark_version: str
    benchmark_hash: str
    is_demo: bool
    protocol_version: str
    score: float
    answered_accuracy: float | None
    completion_rate: float
    average_latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost: float | None
    started_at: datetime | None
    finished_at: datetime | None


class LeaderboardList(ORMModel):
    items: list[LeaderboardEntry]
    total: int
    offset: int
    limit: int


class DashboardSummary(ORMModel):
    model_count: int
    benchmark_count: int
    run_count: int
    completed_run_count: int
    failed_run_count: int
    average_score: float | None
    average_latency_ms: float | None
    total_input_tokens: int | None
    total_output_tokens: int | None
    total_estimated_cost: float | None
    recent_runs: list[LeaderboardEntry]
