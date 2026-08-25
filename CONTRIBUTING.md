# Contributing to LLMBenchLab

Thank you for helping build a lightweight, reproducible LLM evaluation platform. Contributions should remain small enough to review, work offline with the Mock adapter, and preserve the evidence needed to reproduce every score.

## Before you begin

Read `AGENTS.md`, `docs/PROJECT_STATUS.md`, `docs/ROADMAP.md`, and the current phase document. For changes to scoring, datasets, adapters, public APIs, persistence, or security boundaries, also read the relevant protocol and ADRs before proposing an implementation.

Use an issue to describe non-trivial defects or features. Keep proposals within the current roadmap phase and clearly separate the first useful increment from later ideas. Never put API keys, private prompts, confidential model output, or proprietary datasets in an issue.

## Local development

Prerequisites are Python 3.11 or newer, uv, Node.js 22 or newer, and npm. Docker Compose is optional.

```bash
git clone https://github.com/YOUR_ACCOUNT/LLMBenchLab.git
cd LLMBenchLab
make setup
make dev
```

`make setup` creates `.env` only when it is absent; it never overwrites local settings. The default configuration uses SQLite and needs no provider credential. Review `.env.example` before adding an OpenAI-compatible provider, and store only an environment variable name in LLMBenchLab—not the secret value.

Useful focused commands are:

```bash
make backend
make frontend
make migrate
make lint
make test
make smoke
```

`make dev` runs the two development servers in one terminal. `make backend` and `make frontend` are available when separate terminals are more convenient.

## Change discipline

- Preserve unrelated work and avoid broad formatting changes.
- Add tests for new behavior and a regression test for every bug fix.
- Keep models, API schemas, adapters, evaluators, and runner orchestration separated.
- Persist raw output separately from parsed answers and scoring evidence.
- Do not weaken strict-score semantics: every planned question remains in the denominator, while completion rate and answered accuracy are reported independently.
- Treat Demo results as demonstration data, never as a formal model claim.
- Update Alembic for schema changes and document forward and rollback implications.
- Version changes to the Benchmark protocol or dataset format; never silently mix incompatible results.
- Avoid new production dependencies unless their purpose, version, license, and maintenance trade-off are documented.

Follow the planning and work-log process in `AGENTS.md`. Update the affected API, security, testing, deployment, or protocol documentation in the same pull request. Project status must report verified facts only.

## Tests and quality gates

Before opening a pull request, run the checks relevant to the change. The full expected local gate is:

```bash
make lint
make test
make smoke
```

Frontend changes must pass ESLint, TypeScript checking, Vitest, and the production build. Backend changes must pass Ruff lint and format checks plus pytest. End-to-end tests and CI must use the Mock adapter with an isolated temporary SQLite database and must never call a paid or real provider API.

For deployment changes, also run `docker compose config`. If a check cannot run in your environment, include the command, reason, and remaining risk in the pull request; do not report it as passing.

## Pull requests

Create a focused branch in your fork, use clear commits, and complete the pull request template. A reviewable pull request explains its objective and exclusions, links its issue or ADR when applicable, lists exact validation commands and outcomes, and calls out compatibility and security impact.

Maintainers may request a smaller scope when a change combines unrelated behavior or prematurely implements a later roadmap phase. By contributing, you agree that your contribution is licensed under the repository's MIT License.

## Security reports

Do not open a public issue containing an exploitable secret or sensitive data. Redact credentials immediately and contact the repository maintainers privately through the security contact published for the repository. Until a private channel is published, share only a minimal, non-sensitive notice asking a maintainer how to proceed.
