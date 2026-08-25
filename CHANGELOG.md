# Changelog

All notable changes to this project will be documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versioned releases follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

Phase 0 and the Phase 1 MVP vertical slice are complete. The Phase 2 reliable-execution foundation was implemented and fault-tested on 2026-08-25, while Phase 2 as a whole remains `in_progress`. This is a development baseline, not a published release or production/HA claim.

### Added

- Phase 0 governance, architecture decisions, protocols, roadmap, and repeatable work-log process.
- FastAPI, SQLAlchemy, Alembic, SQLite, and Mock-adapter backend for the reproducible MVP evaluation flow.
- React and TypeScript interface for models, Benchmarks, runs, result evidence, summary metrics, and the leaderboard.
- Offline unit, integration, component, and vertical-slice smoke tests that require no provider credential.
- Migration regression tests for clean databases, legacy-data preservation, consistent SQLite backups, schema-drift rejection, idempotency, and startup revision checks.
- Local setup and development scripts, unified Make targets, and an optional two-service Docker Compose deployment with persistent SQLite storage.
- GitHub Actions checks for backend lint/tests and frontend lint/tests/build, plus issue and pull request templates.
- MIT License and contribution guidance for the initial open-source repository.
- Alembic revision `20260825_0002`, PostgreSQL deployment support, database-clock Run leases, monotonic fencing tokens, heartbeats, bounded retry/dead-letter metadata, and idempotent per-question persistence.
- Redis Streams at-least-once notifications plus an independent Worker that reconciles the database, claims leases, resumes missing Responses, and acknowledges messages only after database disposition.
- `/live`, DB-only `/health`, dependency-aware `/ready`, database-derived task gauges, request/Run correlation, and sanitized JSON for LLMBenchLab application loggers.
- A six-service local Compose topology (`postgres`, `redis`, one-shot `migrate`, `api`, `worker`, and `frontend`) and CI jobs for SQLite, real PostgreSQL/Redis integration, and full-stack fault acceptance.
- An explicit stopped-SQLite to offline-empty-PostgreSQL importer with read-only source validation, transactional locking/copy, content-free reconciliation digests, and distinct rollback/commit-uncertainty/post-commit-verification outcomes.

### Changed

- Standardized `score`, `completion_rate`, and `answered_accuracy` as 0–100 values across implementation, API documentation, protocol, and ADR.
- Configured local CORS for both `localhost:5173` and `127.0.0.1:5173`, while continuing to reject wildcard origins.
- Made Run snapshots authoritative for historical model/provider/pricing/execution display, isolated leaderboard ranking by Benchmark protocol/version/Hash, and kept unknown usage or pricing as `null` instead of silently treating it as zero.
- Made Alembic the sole runtime schema owner; backend startup now requires the database at migration head, while setup, migrate, and container startup share the same guarded preflight.
- Moved Run execution ownership out of the API process. `POST /api/v1/runs` now commits database truth before a best-effort queue notification; Redis loss does not erase or decide task state.
- Replaced startup-time failure of all `running` Runs with lease expiry, database reconciliation, fenced recovery, cancellation convergence, and bounded terminal failure semantics.
- Kept `llmbenchlab-protocol-v1` scoring, completion, accuracy, token, cost, and leaderboard meanings unchanged while adding operational reliability fields to Run snapshots and API responses.
- Changed Compose from a two-service SQLite demonstration to a PostgreSQL/Redis reliable-development topology. PostgreSQL and Redis remain internal; API and frontend ports bind to loopback by default.

### Fixed

- Fixed `make setup` failing with `table models already exists` after an earlier development startup had created unversioned tables. Supported SQLite layouts are now integrity-checked, consistently backed up, stamped only to their verified revision, and upgraded without dropping existing models, Benchmarks, questions, runs, or responses; unknown/partial schemas are rejected before stamping.
- Prevented duplicate delivery, stale lease owners, cancellation races, and ACK-result uncertainty from duplicating Responses or changing terminal protocol-v1 aggregates.
- Distinguished SQLite-import failures before commit (exit 2), an unconfirmed PostgreSQL `COMMIT` outcome (exit 4), and failures after a confirmed commit (exit 3), so operators are not told to retry data that may already exist.
- Made the Phase 2 acceptance harness normalize PostgreSQL fractional seconds with 1–6 digits on Python 3.9; the first failing final run and its successful cleanup remain recorded before the corrected 8/8 rerun.

### Security

- Restricted Benchmark ZIP import by size, entry name/type, compression ratio, schema, and fixed root filenames; dataset contents are never executed.
- Kept provider secret values out of persistence and API schemas; provider/network errors are bounded and redacted.
- Rejected Mock remote fields, URL credentials/query/fragment, all unsupported Model default-parameter keys, reflected validation inputs, and non-finite numeric values.
- Documented that arbitrary compatible-provider URLs remain an SSRF risk and that the unauthenticated MVP must not be exposed publicly.
- Kept credentialed importer DSNs out of argv via `--target-env`, rejected passwords in `--target`, and emitted only row counts and SHA-256 reconciliation digests rather than imported row contents.
- Limited published Compose ports to loopback and kept PostgreSQL/Redis off the host network by default. This does not add authentication, TLS, tenant isolation, or production hardening.
- Documented the at-least-once boundary: local database evidence is idempotent, but a Worker crash after a Provider response and before local commit can repeat an upstream call or charge.

### Verification

- Passed 205 backend non-infrastructure tests, five real PostgreSQL/Redis integration tests, 13 frontend tests, the isolated offline Mock smoke test, Ruff/ESLint/TypeScript checks, Vite production build, and SQLite/PostgreSQL Alembic upgrade/check/downgrade/upgrade gates.
- Passed an isolated default-build Compose acceptance harness in all eight scenarios: topology/readiness, protocol-v1 baseline, API restart during execution, exact lease-owner SIGKILL and natural takeover, Redis stop/start with database reconciliation, pending cancellation, running cancellation plus duplicate delivery, and PostgreSQL `head -> 0001 -> head` protocol round-trip.
- Verified final Redis consumer-group `pending=0` and `lag=0`, unchanged three-point canonical hashes for one 15-question baseline Run's protocol-v1 core fields and its 15 Responses, and cleanup with no project containers, volumes, or networks left behind. This hash is not a whole-database snapshot.
- Verified the SQLite importer against PostgreSQL 16 for success, pre-commit rollback, two-source contention, commit acknowledgement loss, post-commit snapshot/output failure, secret-safe CLI behavior, and cleanup of every random target database and temporary container.
