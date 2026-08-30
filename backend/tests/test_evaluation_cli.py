"""Offline safety and orchestration tests for the trusted-local evaluation CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import app.cli.evaluate as evaluation_cli
from app.core.constants import MAX_GENERATION_TOKENS
from app.core.time import utc_now
from app.models import ProviderType, RunStatus
from app.providers import CanaryResult, ModelDiscoveryResult


def _parse_run(*extra: str) -> argparse.Namespace:
    return evaluation_cli.build_parser().parse_args(
        [
            "run",
            "--dataset",
            "mmlu-pro",
            "--limit",
            "2",
            "--base-url",
            "https://provider.example/v1",
            "--model",
            "remote-model",
            *extra,
        ]
    )


def _fake_prepared(tmp_path: Path, *, question_count: int = 2) -> SimpleNamespace:
    dataset = SimpleNamespace(
        dataset_hash="d" * 64,
        manifest={
            "id": "offline-standard-fixture",
            "version": "1.2.3",
            "question_count": question_count,
            "source": "https://datasets.example.invalid/pinned",
            "license": "fixture-only",
        },
    )
    return SimpleNamespace(
        archive_path=tmp_path / "offline-standard-fixture.zip",
        loaded_dataset=dataset,
        archive_sha256="a" * 64,
        source_metadata={"revision": "pinned-fixture-revision", "network": False},
    )


def _canary() -> CanaryResult:
    return CanaryResult(
        model="remote-model",
        returned_model="remote-model",
        system_fingerprint="fixture-fingerprint",
        finish_reason="stop",
        provider_request_id="fixture-request",
        input_tokens=4,
        output_tokens=1,
        latency_ms=2.5,
        attempts=1,
    )


def _all_option_strings(parser: argparse.ArgumentParser) -> set[str]:
    options: set[str] = set()
    pending = [parser]
    while pending:
        current = pending.pop()
        for action in current._actions:
            options.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                pending.extend(action.choices.values())
    return options


def test_help_exposes_only_api_key_environment_name(capsys: pytest.CaptureFixture[str]) -> None:
    parser = evaluation_cli.build_parser()

    with pytest.raises(SystemExit) as top_level:
        parser.parse_args(["--help"])
    assert top_level.value.code == 0
    assert "API keys are never accepted as command-line arguments" in (capsys.readouterr().out)

    with pytest.raises(SystemExit) as caught:
        parser.parse_args(["run", "--help"])

    assert caught.value.code == 0
    help_text = capsys.readouterr().out
    assert "--api-key-env" in help_text
    assert "--api-key" not in _all_option_strings(parser)


@pytest.mark.parametrize(
    "provider_type",
    [ProviderType.OPENAI_RESPONSES, ProviderType.ANTHROPIC_MESSAGES],
)
def test_new_provider_protocol_defaults_omit_seed(provider_type: ProviderType) -> None:
    args = _parse_run("--provider-type", provider_type.value)

    defaults = evaluation_cli._provider_defaults(args)

    assert defaults == {
        "temperature": None,
        "top_p": None,
        "max_tokens": 4000,
        "seed": None,
        "concurrency": 1,
    }


@pytest.mark.parametrize(
    "provider_type",
    [ProviderType.OPENAI_RESPONSES, ProviderType.ANTHROPIC_MESSAGES],
)
def test_new_provider_protocol_preserves_explicit_sampling_parameters(
    provider_type: ProviderType,
) -> None:
    args = _parse_run(
        "--provider-type",
        provider_type.value,
        "--temperature",
        "0.5",
        "--top-p",
        "0.75",
    )

    defaults = evaluation_cli._provider_defaults(args)

    assert defaults["temperature"] == 0.5
    assert defaults["top_p"] == 0.75


@pytest.mark.parametrize(
    "provider_type",
    [ProviderType.OPENAI_RESPONSES, ProviderType.ANTHROPIC_MESSAGES],
)
def test_new_provider_protocol_rejects_explicit_cli_seed(
    provider_type: ProviderType,
) -> None:
    args = _parse_run(
        "--provider-type",
        provider_type.value,
        "--generation-seed",
        "7",
    )

    with pytest.raises(evaluation_cli.EvaluationCLIError, match="does not support"):
        evaluation_cli._provider_defaults(args)


def test_messages_protocol_rejects_temperature_above_one_in_cli() -> None:
    args = _parse_run(
        "--provider-type",
        ProviderType.ANTHROPIC_MESSAGES.value,
        "--temperature",
        "1.5",
    )

    with pytest.raises(evaluation_cli.EvaluationCLIError, match="between 0 and 1"):
        evaluation_cli._provider_defaults(args)


def test_model_payload_persists_explicit_provider_protocol() -> None:
    payload = evaluation_cli._model_payload(
        base_url="https://provider.example/zen/go/v1/responses",
        remote_model="remote-model",
        api_key_env="CLI_PROVIDER_KEY",
        display_name=None,
        input_price=None,
        output_price=None,
        provider_type=ProviderType.OPENAI_RESPONSES,
    )

    assert payload.provider_type == ProviderType.OPENAI_RESPONSES


@pytest.mark.parametrize(
    "argv",
    [
        [
            "run",
            "--dataset",
            "gpqa-diamond",
            "--limit",
            "1",
            "--base-url",
            "https://provider.example/v1",
            "--model",
            "remote-model",
            "--api-key",
            "not-a-real-key",
        ],
        ["resume", "fixture-run", "--api-key", "not-a-real-key"],
    ],
)
def test_api_key_value_option_cannot_abbreviate_api_key_env(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        evaluation_cli.build_parser().parse_args(argv)
    assert caught.value.code == 2


def test_secret_environment_copies_source_then_restores_existing_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLI_SOURCE_KEY", "offline-source-secret")
    monkeypatch.setenv("CLI_TARGET_KEY", "previous-target-value")

    with evaluation_cli._secret_environment(
        "CLI_TARGET_KEY", source_env="CLI_SOURCE_KEY"
    ) as secret:
        assert secret == "offline-source-secret"
        assert evaluation_cli.os.environ["CLI_TARGET_KEY"] == "offline-source-secret"

    assert evaluation_cli.os.environ["CLI_TARGET_KEY"] == "previous-target-value"
    assert evaluation_cli.os.environ["CLI_SOURCE_KEY"] == "offline-source-secret"


def test_secret_environment_removes_temporary_target_after_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLI_SOURCE_KEY", "offline-source-secret")
    monkeypatch.delenv("CLI_TEMPORARY_TARGET", raising=False)

    with (
        pytest.raises(LookupError, match="fixture failure"),
        evaluation_cli._secret_environment("CLI_TEMPORARY_TARGET", source_env="CLI_SOURCE_KEY"),
    ):
        assert evaluation_cli.os.environ["CLI_TEMPORARY_TARGET"] == ("offline-source-secret")
        raise LookupError("fixture failure")

    assert "CLI_TEMPORARY_TARGET" not in evaluation_cli.os.environ


def test_secret_environment_uses_hidden_prompt_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []
    monkeypatch.delenv("CLI_PROMPTED_KEY", raising=False)
    monkeypatch.setattr(
        evaluation_cli.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: True),
    )
    monkeypatch.setattr(
        evaluation_cli.getpass,
        "getpass",
        lambda prompt: prompts.append(prompt) or "offline-prompted-secret",
    )

    with evaluation_cli._secret_environment("CLI_PROMPTED_KEY") as secret:
        assert secret == "offline-prompted-secret"
        assert evaluation_cli.os.environ["CLI_PROMPTED_KEY"] == "offline-prompted-secret"

    assert prompts == ["API key for CLI_PROMPTED_KEY (input hidden): "]
    assert "CLI_PROMPTED_KEY" not in evaluation_cli.os.environ


def test_secret_environment_refuses_missing_key_without_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLI_MISSING_KEY", raising=False)
    monkeypatch.setattr(
        evaluation_cli.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: False),
    )
    monkeypatch.setattr(
        evaluation_cli.getpass,
        "getpass",
        lambda _prompt: pytest.fail("getpass must not run without an interactive terminal"),
    )

    with (
        pytest.raises(evaluation_cli.EvaluationCLIError, match="no interactive terminal"),
        evaluation_cli._secret_environment("CLI_MISSING_KEY"),
    ):
        pytest.fail("the secret context must not be entered")

    assert "CLI_MISSING_KEY" not in evaluation_cli.os.environ


@pytest.mark.parametrize("invalid_name", ["", "9STARTS_WITH_DIGIT", "HAS-DASH", "HAS SPACE"])
def test_secret_environment_rejects_invalid_target_name(invalid_name: str) -> None:
    with (
        pytest.raises(evaluation_cli.EvaluationCLIError, match="variable name is invalid"),
        evaluation_cli._secret_environment(invalid_name),
    ):
        pytest.fail("an invalid environment name must never be installed")


def test_real_call_confirmation_prints_conservative_request_upper_bound(
    capsys: pytest.CaptureFixture[str],
) -> None:
    evaluation_cli._confirm_real_calls(
        "remote-model",
        "https://provider.example/v1/chat/completions",
        100,
        run_attempts=3,
        yes=True,
    )

    message = capsys.readouterr().err
    assert "Provider host: provider.example" in message
    assert "Remote model: remote-model" in message
    assert "Scored questions: 100" in message
    assert "Remaining Run attempts included in upper bound: 3" in message
    assert "Maximum billed Chat Completion attempts this invocation: 903" in message
    assert "cannot enforce a global Provider budget" in message
    assert "stop the regular API/Worker stack" in message


def test_real_call_confirmation_refuses_noninteractive_execution_before_canary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        evaluation_cli.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: False),
    )

    with pytest.raises(evaluation_cli.EvaluationCLIError, match="pass --yes"):
        evaluation_cli._confirm_real_calls(
            "remote-model",
            "https://provider.example/v1",
            2,
            run_attempts=3,
            yes=False,
        )

    assert "Maximum billed Chat Completion attempts this invocation: 21" in (
        capsys.readouterr().err
    )


def test_real_call_confirmation_requires_exact_run_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        evaluation_cli.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: True),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    with pytest.raises(evaluation_cli.EvaluationCLIError, match="no billed canary was sent"):
        evaluation_cli._confirm_real_calls(
            "remote-model",
            "https://provider.example/v1",
            1,
            run_attempts=3,
            yes=False,
        )


def test_provider_defaults_follow_profile_and_validate_overrides() -> None:
    official = evaluation_cli._provider_defaults(_parse_run())
    assert official == {
        "temperature": 0.0,
        "top_p": 1.0,
        "max_tokens": 4000,
        "seed": 42,
        "concurrency": 1,
    }

    direct = evaluation_cli._provider_defaults(
        evaluation_cli.build_parser().parse_args(
            [
                "run",
                "--dataset",
                "gpqa-diamond",
                "--full",
                "--base-url",
                "https://provider.example/v1",
                "--model",
                "remote-model",
                "--temperature",
                "1.25",
                "--top-p",
                "0.5",
                "--max-tokens",
                "777",
                "--no-seed",
                "--concurrency",
                "4",
            ]
        )
    )
    assert direct == {
        "temperature": 1.25,
        "top_p": 0.5,
        "max_tokens": 777,
        "seed": None,
        "concurrency": 4,
    }


@pytest.mark.parametrize(
    "extra",
    [
        ("--temperature", "2.1"),
        ("--top-p", "0"),
        ("--max-tokens", str(MAX_GENERATION_TOKENS + 1)),
        ("--generation-seed", str(2**31)),
    ],
)
def test_provider_defaults_reject_out_of_protocol_values(extra: tuple[str, str]) -> None:
    with pytest.raises(ValidationError):
        evaluation_cli._provider_defaults(_parse_run(*extra))


def test_prepare_dataset_dispatches_filters_without_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sentinel = object()
    calls: list[dict[str, Any]] = []

    def fake_prepare(output_dir: Path, **kwargs: Any) -> object:
        calls.append({"output_dir": output_dir, **kwargs})
        return sentinel

    monkeypatch.setattr(evaluation_cli, "prepare_mmlu_pro", fake_prepare)
    args = evaluation_cli.build_parser().parse_args(
        [
            "prepare",
            "--dataset",
            "mmlu-pro",
            "--groups",
            "math, physics,math",
            "--profile",
            "direct",
            "--limit",
            "3",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--benchmark-dir",
            str(tmp_path / "benchmarks"),
        ]
    )

    assert evaluation_cli._prepare_dataset(args) is sentinel
    assert calls == [
        {
            "output_dir": (tmp_path / "benchmarks").resolve(),
            "cache_dir": (tmp_path / "cache").resolve(),
            "profile": "direct",
            "categories": ("math", "physics"),
            "limit": 3,
        }
    ]


def test_prepare_subcommand_prints_reproducibility_summary_offline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared = _fake_prepared(tmp_path)
    monkeypatch.setattr(evaluation_cli, "_prepare_dataset", lambda _args: prepared)

    with pytest.raises(SystemExit) as caught:
        evaluation_cli.main(
            [
                "prepare",
                "--dataset",
                "gpqa-diamond",
                "--limit",
                "2",
                "--cache-dir",
                str(tmp_path / "cache"),
                "--benchmark-dir",
                str(tmp_path / "benchmarks"),
            ]
        )

    assert caught.value.code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "archive_path": str(prepared.archive_path),
        "archive_sha256": "a" * 64,
        "dataset_hash": "d" * 64,
        "slug": "offline-standard-fixture",
        "version": "1.2.3",
        "question_count": 2,
        "source": "https://datasets.example.invalid/pinned",
        "license": "fixture-only",
        "source_evidence": {
            "revision": "pinned-fixture-revision",
            "network": False,
        },
    }


@pytest.mark.asyncio
async def test_resolve_and_canary_does_not_call_provider_when_confirmation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        evaluation_cli.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: False),
    )

    async def unexpected_canary(*_args: Any, **_kwargs: Any) -> CanaryResult:
        pytest.fail("the billed canary must occur only after confirmation")

    monkeypatch.setattr(evaluation_cli, "run_chat_canary", unexpected_canary)

    with pytest.raises(evaluation_cli.EvaluationCLIError, match="pass --yes"):
        await evaluation_cli._resolve_and_canary(
            base_url="https://provider.example/v1",
            requested_model="remote-model",
            api_key="offline-secret",
            api_key_env="CLI_PROVIDER_KEY",
            generation={"temperature": 0, "top_p": 1, "max_tokens": 64, "seed": 42},
            skip_discovery=True,
            question_count=2,
            run_attempts=3,
            yes=False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["base_url", "requested_model"])
async def test_resolve_and_canary_rejects_key_in_configuration_before_network(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    secret = "fixture-provider-key-value"

    async def unexpected_discovery(*_args: Any, **_kwargs: Any) -> ModelDiscoveryResult:
        pytest.fail("unsafe configuration must be rejected before discovery")

    async def unexpected_canary(*_args: Any, **_kwargs: Any) -> CanaryResult:
        pytest.fail("unsafe configuration must be rejected before canary")

    monkeypatch.setattr(evaluation_cli, "discover_models", unexpected_discovery)
    monkeypatch.setattr(evaluation_cli, "run_chat_canary", unexpected_canary)
    arguments: dict[str, Any] = {
        "base_url": "https://provider.example/v1",
        "requested_model": "remote-model",
        "api_key": secret,
        "api_key_env": "CLI_PROVIDER_KEY",
        "generation": {"temperature": 0, "top_p": 1, "max_tokens": 64, "seed": 42},
        "skip_discovery": False,
        "question_count": 2,
        "run_attempts": 3,
        "yes": True,
    }
    arguments[field] = (
        f"https://provider.example/{secret}/v1" if field == "base_url" else f"model-{secret}"
    )

    with pytest.raises(evaluation_cli.EvaluationCLIError) as caught:
        await evaluation_cli._resolve_and_canary(**arguments)

    assert secret not in str(caught.value)


@pytest.mark.asyncio
async def test_resolve_and_canary_uses_discovery_but_passes_only_env_name_to_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    events: list[str] = []

    async def fake_discovery(
        base_url: str,
        api_key: str,
        *,
        provider_type: ProviderType,
    ) -> ModelDiscoveryResult:
        events.append("discovery")
        seen["discovery"] = (base_url, api_key, provider_type)
        return ModelDiscoveryResult(models=("only-model",), request_id="fixture-discovery")

    async def fake_canary(
        base_url: str,
        remote_model: str,
        api_key_env: str,
        generation: dict[str, Any],
    ) -> CanaryResult:
        events.append("canary")
        seen["canary"] = (base_url, remote_model, api_key_env, generation)
        return _canary()

    def before_canary(remote_model: str) -> None:
        events.append("local-validation")
        assert remote_model == "only-model"

    monkeypatch.setattr(evaluation_cli, "discover_models", fake_discovery)
    monkeypatch.setattr(evaluation_cli, "run_chat_canary", fake_canary)

    remote_model, discovery, canary = await evaluation_cli._resolve_and_canary(
        base_url="https://provider.example/v1",
        requested_model=None,
        api_key="offline-secret",
        api_key_env="CLI_PROVIDER_KEY",
        generation={"temperature": 0, "top_p": 1, "max_tokens": 64, "seed": 42},
        skip_discovery=False,
        question_count=2,
        run_attempts=3,
        yes=True,
        before_canary=before_canary,
    )

    assert remote_model == "only-model"
    assert discovery is not None
    assert canary == _canary()
    assert events == ["discovery", "local-validation", "canary"]
    assert seen["discovery"] == (
        "https://provider.example/v1",
        "offline-secret",
        ProviderType.OPENAI_COMPATIBLE,
    )
    assert seen["canary"] == (
        "https://provider.example/v1",
        "only-model",
        "CLI_PROVIDER_KEY",
        {"temperature": 0, "top_p": 1, "max_tokens": 64, "seed": 42},
    )


@pytest.mark.asyncio
async def test_resolve_and_canary_uses_explicit_non_chat_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def unexpected_chat_canary(*_args: Any, **_kwargs: Any) -> CanaryResult:
        pytest.fail("a Responses Run must not use the Chat Completions canary")

    async def fake_provider_canary(
        provider_type: ProviderType,
        base_url: str,
        remote_model: str,
        api_key_env: str,
        generation: dict[str, Any],
    ) -> CanaryResult:
        seen["canary"] = (
            provider_type,
            base_url,
            remote_model,
            api_key_env,
            generation,
        )
        return _canary()

    monkeypatch.setattr(evaluation_cli, "run_chat_canary", unexpected_chat_canary)
    monkeypatch.setattr(evaluation_cli, "run_provider_canary", fake_provider_canary)

    remote_model, discovery, canary = await evaluation_cli._resolve_and_canary(
        base_url="https://provider.example/zen/go/v1/responses",
        requested_model="remote-model",
        api_key="offline-secret",
        api_key_env="CLI_PROVIDER_KEY",
        generation={"temperature": 0, "top_p": 1, "max_tokens": 64, "seed": None},
        skip_discovery=True,
        question_count=2,
        run_attempts=3,
        yes=True,
        provider_type=ProviderType.OPENAI_RESPONSES,
    )

    assert remote_model == "remote-model"
    assert discovery is None
    assert canary == _canary()
    assert seen["canary"] == (
        ProviderType.OPENAI_RESPONSES,
        "https://provider.example/zen/go/v1/responses",
        "remote-model",
        "CLI_PROVIDER_KEY",
        {"temperature": 0, "top_p": 1, "max_tokens": 64, "seed": None},
    )


def test_resume_reads_explicit_provider_protocol_snapshot() -> None:
    run = SimpleNamespace(
        model_parameters_snapshot={
            "model": {
                "adapter_type": "anthropic_messages",
                "base_url": "https://provider.example/zen/go/v1/messages",
                "remote_model_name": "remote-model",
                "api_key_env": "CLI_PROVIDER_KEY",
            },
            "generation": {
                "temperature": 0,
                "top_p": 1,
                "max_tokens": 64,
                "seed": None,
            },
        }
    )

    provider_type, base_url, remote_model, api_key_env, generation = (
        evaluation_cli._run_provider_configuration(run)
    )

    assert provider_type == ProviderType.ANTHROPIC_MESSAGES
    assert base_url.endswith("/messages")
    assert remote_model == "remote-model"
    assert api_key_env == "CLI_PROVIDER_KEY"
    assert generation["seed"] is None


@pytest.mark.asyncio
async def test_run_refuses_active_database_run_before_dataset_or_provider_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def active_run() -> None:
        events.append("active-check")
        raise evaluation_cli.EvaluationCLIError("fixture Run is already running")

    monkeypatch.setattr(evaluation_cli, "_ensure_no_active_run", active_run)
    monkeypatch.setattr(
        evaluation_cli,
        "_prepare_dataset",
        lambda _args: pytest.fail("dataset preparation must not precede the active-Run check"),
    )
    monkeypatch.setattr(
        evaluation_cli,
        "_secret_environment",
        lambda *_args, **_kwargs: pytest.fail("an active Run must not access a Provider key"),
    )

    with pytest.raises(evaluation_cli.EvaluationCLIError, match="already running"):
        await evaluation_cli._run_new(
            _parse_run("--yes"),
            SimpleNamespace(worker_max_attempts=3),
        )

    assert events == ["active-check"]


@pytest.mark.asyncio
async def test_run_orchestration_is_offline_and_never_persists_key_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared = _fake_prepared(tmp_path)
    events: list[str] = []
    persisted: dict[str, Any] = {}
    monkeypatch.setenv("CLI_REAL_PROVIDER_KEY", "offline-provider-secret")
    args = _parse_run(
        "--api-key-env",
        "CLI_REAL_PROVIDER_KEY",
        "--yes",
        "--report-dir",
        str(tmp_path / "report"),
    )

    def fake_prepare(_args: argparse.Namespace) -> object:
        events.append("prepare")
        return prepared

    async def fake_preflight(**kwargs: Any) -> tuple[str, None, CanaryResult]:
        events.append("preflight")
        assert kwargs["api_key"] == "offline-provider-secret"
        assert kwargs["api_key_env"] == "CLI_REAL_PROVIDER_KEY"
        assert kwargs["run_attempts"] == 3
        kwargs["before_canary"]("remote-model")
        return "remote-model", None, _canary()

    def fake_persist(_settings: object, _prepared: object, **kwargs: Any) -> object:
        events.append("persist")
        persisted.update(kwargs)
        assert "api_key" not in kwargs
        return SimpleNamespace(id="fixture-run")

    async def fake_drive(run_id: str, _settings: object) -> object:
        events.append("drive")
        assert run_id == "fixture-run"
        return SimpleNamespace(
            id=run_id,
            status=RunStatus.COMPLETED,
            score=50.0,
            completion_rate=100.0,
            answered_accuracy=50.0,
        )

    def fake_export(run_id: str, output: Path, **kwargs: Any) -> object:
        events.append("report")
        assert run_id == "fixture-run"
        assert output == (tmp_path / "report").resolve()
        assert kwargs == {"secret_values": ("offline-provider-secret",)}
        return SimpleNamespace(directory=output)

    monkeypatch.setattr(evaluation_cli, "_prepare_dataset", fake_prepare)
    monkeypatch.setattr(evaluation_cli, "_ensure_no_active_run", lambda: events.append("ensure"))
    monkeypatch.setattr(
        evaluation_cli,
        "_validate_model_registration",
        lambda **_kwargs: events.append("validate-model"),
    )
    monkeypatch.setattr(evaluation_cli, "_resolve_and_canary", fake_preflight)
    monkeypatch.setattr(evaluation_cli, "_create_persisted_run", fake_persist)
    monkeypatch.setattr(evaluation_cli, "_drive_run", fake_drive)
    monkeypatch.setattr(evaluation_cli, "_export", fake_export)

    status = await evaluation_cli._run_new(args, SimpleNamespace(worker_max_attempts=3))

    assert status == 0
    assert events == [
        "ensure",
        "prepare",
        "preflight",
        "validate-model",
        "persist",
        "drive",
        "report",
    ]
    assert persisted["api_key_env"] == "CLI_REAL_PROVIDER_KEY"
    assert "offline-provider-secret" not in json.dumps(persisted, default=str)
    assert evaluation_cli.os.environ["CLI_REAL_PROVIDER_KEY"] == "offline-provider-secret"
    output = json.loads(capsys.readouterr().out)
    assert output["run_id"] == "fixture-run"
    assert output["status"] == "completed"
    assert output["report_directory"] == str((tmp_path / "report").resolve())


@pytest.mark.asyncio
async def test_resume_temporarily_copies_source_key_and_evaluates_only_missing_questions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    initial = SimpleNamespace(
        id="fixture-resume-run",
        status=RunStatus.PENDING,
        total_questions=10,
        completed_questions=3,
        attempt_count=3,
        failed_attempt_count=0,
        max_attempts=3,
        model_parameters_snapshot={
            "model": {
                "base_url": "https://provider.example/v1",
                "remote_model_name": "remote-model",
                "api_key_env": "CLI_FROZEN_TARGET_KEY",
            },
            "generation": {
                "temperature": 0,
                "top_p": 1,
                "max_tokens": 64,
                "seed": 42,
            },
        },
    )
    completed = SimpleNamespace(
        id=initial.id,
        status=RunStatus.COMPLETED,
        score=70.0,
    )
    args = evaluation_cli.build_parser().parse_args(
        [
            "resume",
            initial.id,
            "--api-key-env",
            "CLI_RESUME_SOURCE_KEY",
            "--yes",
            "--report-dir",
            str(tmp_path / "resumed-report"),
        ]
    )
    monkeypatch.setenv("CLI_RESUME_SOURCE_KEY", "offline-resume-secret")
    monkeypatch.delenv("CLI_FROZEN_TARGET_KEY", raising=False)
    monkeypatch.setattr(evaluation_cli, "_load_run", lambda _run_id: initial)

    async def fake_preflight(**kwargs: Any) -> tuple[str, None, CanaryResult]:
        assert kwargs["api_key"] == "offline-resume-secret"
        assert kwargs["api_key_env"] == "CLI_FROZEN_TARGET_KEY"
        assert kwargs["question_count"] == 7
        assert kwargs["run_attempts"] == 3
        assert evaluation_cli.os.environ["CLI_FROZEN_TARGET_KEY"] == ("offline-resume-secret")
        return "remote-model", None, _canary()

    async def fake_drive(_run_id: str, _settings: object) -> object:
        assert evaluation_cli.os.environ["CLI_FROZEN_TARGET_KEY"] == ("offline-resume-secret")
        return completed

    monkeypatch.setattr(evaluation_cli, "_resolve_and_canary", fake_preflight)
    monkeypatch.setattr(evaluation_cli, "_drive_run", fake_drive)

    def fake_export(_run_id: str, output: Path, **kwargs: Any) -> object:
        assert kwargs == {"secret_values": ("offline-resume-secret",)}
        return SimpleNamespace(directory=output)

    monkeypatch.setattr(evaluation_cli, "_export", fake_export)

    status = await evaluation_cli._resume(args, SimpleNamespace())

    assert status == 0
    assert "CLI_FROZEN_TARGET_KEY" not in evaluation_cli.os.environ
    assert evaluation_cli.os.environ["CLI_RESUME_SOURCE_KEY"] == "offline-resume-secret"
    output = json.loads(capsys.readouterr().out)
    assert output["run_id"] == initial.id
    assert output["score"] == 70.0
    assert output["report_directory"] == str((tmp_path / "resumed-report").resolve())


@pytest.mark.asyncio
async def test_resume_refuses_exhausted_run_before_accessing_key_or_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exhausted = SimpleNamespace(
        id="fixture-exhausted-run",
        status=RunStatus.PENDING,
        total_questions=10,
        completed_questions=5,
        attempt_count=3,
        failed_attempt_count=3,
        max_attempts=3,
        model_parameters_snapshot={
            "model": {
                "base_url": "https://provider.example/v1",
                "remote_model_name": "remote-model",
                "api_key_env": "CLI_EXHAUSTED_KEY",
            },
            "generation": {"temperature": 0, "top_p": 1, "max_tokens": 64, "seed": 42},
        },
    )
    args = evaluation_cli.build_parser().parse_args(["resume", exhausted.id, "--yes"])
    monkeypatch.setattr(evaluation_cli, "_load_run", lambda _run_id: exhausted)
    monkeypatch.setattr(
        evaluation_cli,
        "_secret_environment",
        lambda *_args, **_kwargs: pytest.fail("an exhausted Run must not access its key"),
    )

    with pytest.raises(evaluation_cli.EvaluationCLIError, match="exhausted all 3"):
        await evaluation_cli._resume(args, SimpleNamespace())


@pytest.mark.asyncio
async def test_drive_run_reclaims_an_expired_incomplete_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expired = SimpleNamespace(
        id="expired-run",
        status=RunStatus.RUNNING,
        lease_expires_at=utc_now() - timedelta(seconds=1),
        next_attempt_at=None,
    )
    completed = SimpleNamespace(
        id="expired-run",
        status=RunStatus.COMPLETED,
        lease_expires_at=None,
        next_attempt_at=None,
    )
    loaded = iter((expired, expired, completed))
    events: list[str] = []

    class FakeRepository:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def reap_expired(self) -> None:
            events.append("reap")

    class FakeRunner:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def execute(
            self,
            run_id: str,
            *,
            shutdown_requested: asyncio.Event,
        ) -> bool:
            assert run_id == "expired-run"
            assert shutdown_requested.is_set() is False
            events.append("claim-and-execute")
            return True

    monkeypatch.setattr(evaluation_cli, "RunLeaseRepository", FakeRepository)
    monkeypatch.setattr(evaluation_cli, "EvaluationRunner", FakeRunner)
    monkeypatch.setattr(evaluation_cli, "_load_run", lambda _run_id: next(loaded))

    result = await evaluation_cli._drive_run(
        "expired-run",
        SimpleNamespace(
            worker_lease_seconds=30,
            worker_retry_backoff_base_seconds=1,
            worker_retry_backoff_cap_seconds=10,
            worker_poll_seconds=0.01,
        ),
    )

    assert result is completed
    assert events == ["reap", "claim-and-execute"]


def test_report_subcommand_never_initializes_provider_or_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = SimpleNamespace(id="fixture-report-run", status=RunStatus.COMPLETED)
    initialized: list[bool] = []
    output_dir = (tmp_path / "report-only").resolve()
    monkeypatch.setattr(
        evaluation_cli,
        "initialize_database",
        lambda: initialized.append(True),
    )
    monkeypatch.setattr(evaluation_cli, "_load_run", lambda _run_id: run)
    monkeypatch.setattr(
        evaluation_cli,
        "_export",
        lambda run_id, output, **kwargs: (
            pytest.fail("wrong report arguments")
            if (run_id, output, kwargs) != (run.id, output_dir, {"group_by": "category"})
            else SimpleNamespace(directory=output)
        ),
    )
    monkeypatch.setattr(
        evaluation_cli,
        "_secret_environment",
        lambda *_args, **_kwargs: pytest.fail("report must not access a Provider key"),
    )

    with pytest.raises(SystemExit) as caught:
        evaluation_cli.main(
            [
                "report",
                run.id,
                "--output-dir",
                str(output_dir),
                "--group-by",
                "category",
            ]
        )

    assert caught.value.code == 0
    assert initialized == [True]
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "run_id": run.id,
        "status": "completed",
        "report_directory": str(output_dir),
    }
