## Summary

<!-- Explain the user-visible or engineering outcome and why it is needed. -->

## Scope

<!-- State what is included and explicitly excluded. Link related issues or ADRs. -->

## Verification

<!-- List the exact commands run and their results. Do not claim checks that were not run. -->

```text
make lint
make test
make smoke
```

## Compatibility and risk

<!-- Cover database, API, Benchmark protocol, security, reproducibility, and rollback impact. -->

## Checklist

- [ ] The objective is clear and the change has a bounded scope.
- [ ] Relevant automated tests pass, including failure and empty-state paths where applicable.
- [ ] Documentation and user-facing instructions are updated.
- [ ] A forward Alembic migration is included, or no database migration is required.
- [ ] API compatibility was preserved or the documented contract and tests were updated.
- [ ] Benchmark protocol, dataset schema, hashes, and scoring semantics remain compatible, or the versioned protocol documentation was updated.
- [ ] No API keys, `.env` files, Authorization headers, cookies, or private model data are committed or logged.
- [ ] CI and smoke tests use only the Mock adapter and do not call a real model API.
- [ ] `docs/PROJECT_STATUS.md` reflects only verified project state.
- [ ] The current task work log records scope, decisions, commands, results, and known limitations.
- [ ] `CHANGELOG.md` is updated for a user-visible change.
- [ ] I reviewed the final diff for unrelated changes and generated artifacts.

