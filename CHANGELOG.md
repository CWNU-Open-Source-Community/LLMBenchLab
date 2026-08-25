# Changelog

All notable changes to this project will be documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versioned releases follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

Phase 0 and the Phase 1 MVP vertical slice were completed on 2026-08-25. This is a development baseline, not a published release.

### Added

- Phase 0 governance, architecture decisions, protocols, roadmap, and repeatable work-log process.
- FastAPI, SQLAlchemy, Alembic, SQLite, and Mock-adapter backend for the reproducible MVP evaluation flow.
- React and TypeScript interface for models, Benchmarks, runs, result evidence, summary metrics, and the leaderboard.
- Offline unit, integration, component, and vertical-slice smoke tests that require no provider credential.
- Migration regression tests for clean databases, legacy-data preservation, consistent SQLite backups, schema-drift rejection, idempotency, and startup revision checks.
- Local setup and development scripts, unified Make targets, and an optional two-service Docker Compose deployment with persistent SQLite storage.
- GitHub Actions checks for backend lint/tests and frontend lint/tests/build, plus issue and pull request templates.
- MIT License and contribution guidance for the initial open-source repository.

### Changed

- Standardized `score`, `completion_rate`, and `answered_accuracy` as 0–100 values across implementation, API documentation, protocol, and ADR.
- Configured local CORS for both `localhost:5173` and `127.0.0.1:5173`, while continuing to reject wildcard origins.
- Made Run snapshots authoritative for historical model/provider/pricing/execution display, isolated leaderboard ranking by Benchmark protocol/version/Hash, and kept unknown usage or pricing as `null` instead of silently treating it as zero.
- Made Alembic the sole runtime schema owner; backend startup now requires the database at migration head, while setup, migrate, and container startup share the same guarded preflight.

### Fixed

- Fixed `make setup` failing with `table models already exists` after an earlier development startup had created unversioned tables. Supported SQLite layouts are now integrity-checked, consistently backed up, stamped only to their verified revision, and upgraded without dropping existing models, Benchmarks, questions, runs, or responses; unknown/partial schemas are rejected before stamping.

### Security

- Restricted Benchmark ZIP import by size, entry name/type, compression ratio, schema, and fixed root filenames; dataset contents are never executed.
- Kept provider secret values out of persistence and API schemas; provider/network errors are bounded and redacted.
- Rejected Mock remote fields, URL credentials/query/fragment, all unsupported Model default-parameter keys, reflected validation inputs, and non-finite numeric values.
- Documented that arbitrary compatible-provider URLs remain an SSRF risk and that the unauthenticated MVP must not be exposed publicly.

### Verification

- Passed 130 backend tests, 13 frontend tests, the isolated offline Mock smoke test, backend/frontend lint, TypeScript checks, Vite production build, and Alembic upgrade/check/downgrade/upgrade.
- Built both Docker images and verified an isolated Compose stack with healthy backend/frontend services, Nginx proxying, SPA delivery, and a 15/15 production-container Mock vertical slice.
