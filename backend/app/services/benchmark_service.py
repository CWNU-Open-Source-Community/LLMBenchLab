"""Transactional persistence for already validated Benchmark datasets."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Benchmark, Question
from app.services.dataset_loader import LoadedDataset


class BenchmarkConflictError(ValueError):
    """The same benchmark slug/version already exists with different content."""


def persist_dataset(
    session: Session,
    dataset: LoadedDataset,
    *,
    is_demo: bool = False,
) -> tuple[Benchmark, bool]:
    """Persist a validated dataset atomically and return ``(benchmark, created)``."""

    manifest = dataset.manifest
    existing = session.scalar(
        select(Benchmark).where(
            Benchmark.slug == manifest["id"], Benchmark.version == manifest["version"]
        )
    )
    if existing is not None:
        if existing.dataset_hash != dataset.dataset_hash:
            raise BenchmarkConflictError(
                "The benchmark slug/version already exists with a different dataset hash"
            )
        if is_demo and not existing.is_demo:
            existing.is_demo = True
            session.commit()
            session.refresh(existing)
        return existing, False

    evaluator = manifest["evaluator"]
    benchmark = Benchmark(
        slug=manifest["id"],
        name=manifest["name"],
        version=manifest["version"],
        description=manifest["description"],
        dimension=manifest["dimension"],
        language=manifest["language"],
        license=manifest["license"],
        source=manifest["source"],
        evaluator_type=evaluator["name"],
        evaluator_config=evaluator,
        prompt_template=manifest["prompt_template"],
        schema_version=manifest["schema_version"],
        dataset_hash=dataset.dataset_hash,
        question_count=manifest["question_count"],
        is_demo=is_demo,
    )
    session.add(benchmark)
    session.flush()
    for position, record in enumerate(dataset.questions):
        session.add(
            Question(
                benchmark_id=benchmark.id,
                external_id=record["id"],
                position=position,
                question_type=record["type"],
                prompt=record["prompt"],
                choices=record.get("choices"),
                reference_answer=record["answer"],
                evaluator_config=record.get("evaluator_config", {}),
                metadata_=record.get("metadata", {}),
            )
        )
    session.commit()
    session.refresh(benchmark)
    return benchmark, True
