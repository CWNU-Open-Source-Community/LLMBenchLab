"""Validated Benchmark import, discovery, and Demo reload endpoints."""

from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select

from app.api.deps import PaginationDep, SessionDep
from app.models import Benchmark, Question
from app.schemas.benchmark import BenchmarkList, BenchmarkRead
from app.schemas.question import QuestionList
from app.services.benchmark_service import BenchmarkConflictError, persist_dataset
from app.services.dataset_loader import (
    MAX_ARCHIVE_BYTES,
    DatasetValidationError,
    load_dataset_directory,
    load_dataset_zip_bytes,
)

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])
PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEMO_DIRECTORY = PROJECT_ROOT / "benchmarks" / "demo-general"


def _dataset_error(exc: DatasetValidationError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.as_dict()["error"]
    )


@router.get("", response_model=BenchmarkList, summary="分页列出 Benchmark")
def list_benchmarks(
    session: SessionDep,
    pagination: PaginationDep,
    dimension: str | None = None,
    language: str | None = None,
    is_demo: bool | None = None,
) -> BenchmarkList:
    filters = []
    if dimension:
        filters.append(Benchmark.dimension == dimension)
    if language:
        filters.append(Benchmark.language == language)
    if is_demo is not None:
        filters.append(Benchmark.is_demo == is_demo)
    total = session.scalar(select(func.count()).select_from(Benchmark).where(*filters)) or 0
    items = list(
        session.scalars(
            select(Benchmark)
            .where(*filters)
            .order_by(Benchmark.created_at.desc(), Benchmark.id)
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
    )
    return BenchmarkList(items=items, total=total, offset=pagination.offset, limit=pagination.limit)


@router.post(
    "/import",
    response_model=BenchmarkRead,
    status_code=status.HTTP_201_CREATED,
    summary="导入 Benchmark ZIP",
)
async def import_benchmark(
    session: SessionDep,
    archive: UploadFile = File(description="仅含 manifest.json 与 questions.jsonl 的 ZIP"),
) -> Benchmark:
    if archive.content_type not in {"application/zip", "application/x-zip-compressed", None}:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"code": "zip_required", "message": "Benchmark import accepts a ZIP archive"},
        )
    data = await archive.read(MAX_ARCHIVE_BYTES + 1)
    await archive.close()
    if len(data) > MAX_ARCHIVE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": "archive_too_large",
                "message": "Benchmark archive exceeds the size limit",
            },
        )
    try:
        loaded = load_dataset_zip_bytes(data)
        benchmark, _ = persist_dataset(session, loaded)
    except DatasetValidationError as exc:
        session.rollback()
        raise _dataset_error(exc) from exc
    except BenchmarkConflictError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "benchmark_version_conflict", "message": str(exc)},
        ) from exc
    return benchmark


@router.post("/reload-demo", response_model=BenchmarkRead, summary="幂等载入内置 Demo")
def reload_demo(session: SessionDep) -> Benchmark:
    try:
        loaded = load_dataset_directory(DEMO_DIRECTORY)
        benchmark, _ = persist_dataset(session, loaded, is_demo=True)
    except DatasetValidationError as exc:
        session.rollback()
        raise _dataset_error(exc) from exc
    except BenchmarkConflictError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "demo_version_conflict", "message": str(exc)},
        ) from exc
    return benchmark


@router.get("/{benchmark_id}", response_model=BenchmarkRead, summary="查看 Benchmark")
def get_benchmark(benchmark_id: str, session: SessionDep) -> Benchmark:
    benchmark = session.get(Benchmark, benchmark_id)
    if benchmark is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "benchmark_not_found", "message": "Benchmark was not found"},
        )
    return benchmark


@router.get("/{benchmark_id}/questions", response_model=QuestionList, summary="查看题目")
def list_questions(
    benchmark_id: str,
    session: SessionDep,
    pagination: PaginationDep,
    question_type: str | None = Query(default=None),
) -> QuestionList:
    if session.get(Benchmark, benchmark_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "benchmark_not_found", "message": "Benchmark was not found"},
        )
    filters = [Question.benchmark_id == benchmark_id]
    if question_type:
        filters.append(Question.question_type == question_type)
    total = session.scalar(select(func.count()).select_from(Question).where(*filters)) or 0
    items = list(
        session.scalars(
            select(Question)
            .where(*filters)
            .order_by(Question.position)
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
    )
    return QuestionList(items=items, total=total, offset=pagination.offset, limit=pagination.limit)
