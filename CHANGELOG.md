# Changelog

All notable changes to this project will be documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versioned releases follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

Phase 0 and the Phase 1 MVP vertical slice are complete. The Phase 2 reliable-execution foundation and a trusted-local MMLU-Pro/GPQA-Diamond evaluation slice are implemented, while Phase 2 and Phase 3 as a whole remain `in_progress`. This is a development baseline, not a published release or production/HA claim.

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
- Public organization repository at `CWNU-Open-Source-Community/LLMBenchLab`, a visible CI badge, and a repository-wide stage gate requiring each commit to be pushed and its exact GitHub Actions SHA to pass all required jobs.
- Pinned-source MMLU-Pro and GPQA-Diamond converters with source/archive SHA-256 verification, validated caches, deterministic filtering/shuffling, reproducible dataset-v1 ZIPs, and source/license/profile evidence without committing third-party questions.
- A trusted-local `llmbenchlab-evaluate` CLI with `prepare`, `run`, `resume`, and `report`; OpenAI-compatible model discovery and canary preflight; hidden/environment-only API keys; explicit request-bound confirmation; direct database execution; and missing-question recovery. Remote Provider endpoints require HTTPS, while plain HTTP is accepted only for loopback hosts; discovery rejects a model ID that reflects the current Key, and canary rejects a returned model that differs from the requested target.
- Atomic, non-overwriting terminal Run reports containing a protocol/source/model/execution summary, optional metadata groups, and every persisted per-question Response in paginated JSONL. Report metrics are derived from planned questions plus persisted Responses, and `metrics_provenance` identifies drift from persisted Run aggregate fields.
- Web/API write-only Provider credentials: the Models password field accepts an 8–8192-byte visible-ASCII `api_key` directly, never reads it back, and distinguishes `stored`, legacy `environment`, and `none` sources without displaying the legacy environment-variable name. A one-row-per-model `model_credentials` table stores only AES-256-GCM ciphertext, nonce, algorithm and key ID; API and Worker share a deployment keyring while the existing environment-variable and trusted-local CLI paths remain compatible.

### Changed

- Standardized `score`, `completion_rate`, and `answered_accuracy` as 0–100 values across implementation, API documentation, protocol, and ADR.
- Configured local CORS for both `localhost:5173` and `127.0.0.1:5173`, while continuing to reject wildcard origins.
- Made Run snapshots authoritative for historical model/provider/pricing/execution display, isolated leaderboard ranking by Benchmark protocol/version/Hash, and kept unknown usage or pricing as `null` instead of silently treating it as zero.
- Made Alembic the sole runtime schema owner; backend startup now requires the database at migration head, while setup, migrate, and container startup share the same guarded preflight.
- Moved Run execution ownership out of the API process. `POST /api/v1/runs` now commits database truth before a best-effort queue notification; Redis loss does not erase or decide task state.
- Replaced startup-time failure of all `running` Runs with lease expiry, database reconciliation, fenced recovery, cancellation convergence, and bounded terminal failure semantics.
- Kept `llmbenchlab-protocol-v1` scoring, completion, accuracy, token, cost, and leaderboard meanings unchanged while adding operational reliability fields to Run snapshots and API responses.
- Changed Compose from a two-service SQLite demonstration to a PostgreSQL/Redis reliable-development topology. PostgreSQL and Redis remain internal; API and frontend ports bind to loopback by default.
- Raised the dataset-v1 resource ceiling to 20,000 questions, 128 MiB for `questions.jsonl`, and 130 MiB for ZIP archives so the pinned 12,032-question MMLU-Pro test split can be imported while retaining line, compression-ratio, path, and schema controls.
- Reworked Runner question scheduling to at most `concurrency` consumer tasks, moved large snapshot loading off the event loop so the claimed lease continues heartbeating, reused and explicitly closed one OpenAI-compatible HTTP client per Run, and omitted blank system messages for provider compatibility.
- Enriched immutable Run benchmark snapshots with schema version, source, license, dimension, and language while keeping `llmbenchlab-protocol-v1` scoring and API v1 paths unchanged.
- Extended the Alembic chain to `20260827_0003` and the stopped SQLite→PostgreSQL importer from five to six core tables so encrypted `model_credentials` move atomically with their Models. The keyring is deliberately not stored in or copied with the database and must be backed up/transferred separately; downgrade refuses to discard nonempty encrypted credentials.

### Fixed

- Fixed `make setup` failing with `table models already exists` after an earlier development startup had created unversioned tables. Supported SQLite layouts are now integrity-checked, consistently backed up, stamped only to their verified revision, and upgraded without dropping existing models, Benchmarks, questions, runs, or responses; unknown/partial schemas are rejected before stamping.
- Prevented duplicate delivery, stale lease owners, cancellation races, and ACK-result uncertainty from duplicating Responses or changing terminal protocol-v1 aggregates.
- Distinguished SQLite-import failures before commit (exit 2), an unconfirmed PostgreSQL `COMMIT` outcome (exit 4), and failures after a confirmed commit (exit 3), so operators are not told to retry data that may already exist.
- Made the Phase 2 acceptance harness normalize PostgreSQL fractional seconds with 1–6 digits on Python 3.9; the first failing final run and its successful cleanup remain recorded before the corrected 8/8 rerun.
- Recomputed persisted Response evidence before both fail-attempt and expired-lease dead-letter transitions, preventing a partially completed Failed Run from retaining stale zero aggregates.
- Made terminal report summaries, groups, and response evidence use one evidence-derived metric source even when legacy/stale Run aggregate fields differ.
- Let the trusted-local CLI fenced-reclaim an expired, incomplete `running` lease after reaping terminal evidence, preventing `resume` from waiting forever for the deliberately stopped regular Worker.
- Serialized Model credential/endpoint mutation with Run snapshot creation through one dialect-aware lock: PostgreSQL uses `SELECT ... FOR UPDATE`, while SQLite acquires `BEGIN IMMEDIATE` before reading the Model.
- Kept the keyring bootstrap executable under the repository's system Python 3.9 baseline, including its postponed type annotations, while retaining its atomic create/validate/permission behavior.

### Security

- Restricted Benchmark ZIP import by size, entry name/type, compression ratio, schema, and fixed root filenames; dataset contents are never executed.
- Kept Provider plaintext out of persistence and all read schemas; `api_key` exists only as a write-only `SecretStr` request field, while Provider/network errors are bounded and redacted.
- Rejected Mock remote fields, URL credentials/query/fragment, all unsupported Model default-parameter keys, reflected validation inputs, and non-finite numeric values.
- Documented that arbitrary compatible-provider URLs remain an SSRF risk and that the unauthenticated MVP must not be exposed publicly.
- Kept credentialed importer DSNs out of argv via `--target-env`, rejected passwords in `--target`, and emitted only row counts and SHA-256 reconciliation digests rather than imported row contents.
- Limited published Compose ports to loopback and kept PostgreSQL/Redis off the host network by default. This does not add authentication, TLS, tenant isolation, or production hardening.
- Documented the at-least-once boundary: local database evidence is idempotent, but a Worker crash after a Provider response and before local commit can repeat an upstream call or charge.
- Kept real API keys out of argv, plaintext persistence, read API responses, reports, and automated tests. Model discovery is identity-only and capped at 2 MiB; Chat success bodies are capped at 4 MiB and error bodies at 64 KiB. The exact current Key is removed from successful content, raw usage, Provider request IDs, returned model IDs, system fingerprints, and finish reasons before persistence; model discovery/canary errors remain bounded and sanitized.
- Required a typed confirmation before any canary or formal request, showed a conservative HTTP-attempt upper bound, rejected active Runs/disabled or conflicting Models before paid preflight, and documented that this is not a Token or monetary budget.
- Documented the trusted-local exclusivity and SSRF/data-egress boundary: regular API/Worker processes must be stopped before direct CLI execution, and arbitrary compatible-provider URLs remain unsuitable for untrusted/public use.
- Made Web/API keys write-only and short-lived in browser state: the password field is cleared when submission starts, on close/provider switch, and on unmount; pending writes are aborted on close/unmount, exact reflected error text is redacted, and no Key is written to browser storage or console. Credential status is exposed through `credential_source`/`has_api_key` while the legacy environment-variable-name field remains compatible; reads never expose plaintext or encryption material.
- Bound each AES-256-GCM envelope to the Model ID and normalized Provider origin; changing origin requires a new Key, and any Provider endpoint/credential change is rejected while that Model has a pending or running Run. Missing/invalid keyrings, unknown key IDs and authentication failures fail closed with stable non-secret errors.
- Kept the new credential store within the trusted loopback boundary: it adds neither authentication nor a production KMS, and compromise of both the database and deployment keyring can recover Provider keys.
- Prevented a new or preserved stored Key from being copied from the credential flow into any field of the exact `ModelRead` projection or the Run snapshot's derived `model` sub-projection during create/PATCH. Preserved values are decrypted only for this fail-closed comparison; Provider JSON evidence is recursively redacted, including numeric Key values in usage/token and status fields, before persistence.
- Allowed an unreadable stored envelope, including an unknown or retired `key_id`, to be repaired by an isolated explicit new Key or removed by an isolated switch to Mock/legacy environment mode. Recovery requests that also change unrelated public fields fail with a stable `422`; requests that preserve `stored` without a valid replacement fail with a stable `503`; both leave the transaction unchanged.
- Ignored client-controlled `X-Request-ID` values and generated a fresh server UUID for every API response, preventing a caller from duplicating a write-only Key into a reflected/logged correlation header.

### Verification

- Passed 205 backend non-infrastructure tests, five real PostgreSQL/Redis integration tests, 13 frontend tests, the isolated offline Mock smoke test, Ruff/ESLint/TypeScript checks, Vite production build, and SQLite/PostgreSQL Alembic upgrade/check/downgrade/upgrade gates.
- Passed an isolated default-build Compose acceptance harness in all eight scenarios: topology/readiness, protocol-v1 baseline, API restart during execution, exact lease-owner SIGKILL and natural takeover, Redis stop/start with database reconciliation, pending cancellation, running cancellation plus duplicate delivery, and PostgreSQL `head -> 0001 -> head` protocol round-trip.
- Verified final Redis consumer-group `pending=0` and `lag=0`, unchanged three-point canonical hashes for one 15-question baseline Run's protocol-v1 core fields and its 15 Responses, and cleanup with no project containers, volumes, or networks left behind. This hash is not a whole-database snapshot.
- Verified the SQLite importer against PostgreSQL 16 for success, pre-commit rollback, two-source contention, commit acknowledgement loss, post-commit snapshot/output failure, secret-safe CLI behavior, and cleanup of every random target database and temporary container.
- Passed the final Web-credential current-worktree local gate: `421` backend tests passed with `6` explicit infrastructure skips when DSNs were absent; all `6` real PostgreSQL/Redis integration tests passed separately; `21` frontend tests, lint/typecheck, production build, offline Smoke, PostgreSQL Alembic upgrade/check, lock/config checks, and the isolated `8/8` Compose fault acceptance passed.
- Downloaded and validated the complete pinned sources locally: both MMLU-Pro profiles produced 12,032 questions and GPQA-Diamond produced 198; small `llmbenchlab-evaluate prepare --limit 2` runs also passed through the public CLI. No real Provider or API key was used.
- Implementation commit `b19bdac9236f9b2f927166ebe30578ced3d9f53e` was pushed normally. Exact-SHA GitHub Actions validation remains untriggered because the branch has no pull request and the workflow listens only to pull requests/main; local success is not being reported as a substitute.
- Web credential automation uses only marker keys, fixed test keyrings, MockTransport/stub fetch and Mock evaluation. No real Provider was called. Local gates are complete; stage commit/push and exact-SHA CI remain separate remote gates and are not inferred from local success.
- Trusted-loopback browser verification confirmed a password input, no `api_key_env` control, no post-save Key echo, and no fake browser Key in application logs.
