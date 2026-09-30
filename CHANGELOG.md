# Changelog

## [1.0.0] — 2026-09-30

Released after its first real adoption: 1-hive, 3 goals and 138 events, every signature re-verifying (`docs/adoption/1-hive.md`). R1 and R2 are done by the release plan's definition.

- `hive fold` and `hive verify-log`: `--registry` materializes the policy trees a log pins, and a missing tree is a clean error instead of a traceback (SPEC amendment A2).
- SPEC: amendment A1 (`route.` family, core table 1.1.0) and A2 (standalone fold across policy changes).
- `docs/adoption/1-hive.md` rewritten from a plan into the adoption record.

## 1.0.0.dev0

- **Amendment A1** (2026-09-29): the `route.` family (SPEC Appendix D) for the router (R8): `route.table_pinned`, `route.mode_set`, `route.decided`, `route.waiting`, `route.canary_recorded`, `route.drift_detected`, recorded by an `instrument`. Core legality table 1.1.0; fixture `core/routing`; every fixture rebuilt for the new policy pin.

First implementation of SPEC v1.0-draft.5 (R1 + R2).

- **`SPEC.md` frozen as v1.0** (2026-09-28). Later changes are numbered amendments in its Appendix D.

- `SPEC.md` draft.4: signing over canonical body bytes, integer-only numbers (`usd_micros`), bootstrap registering the first operator, proposal application details, registry digest, precise mirror-mode scope, explicit table format, state and fixture format (Appendix B).
- `SPEC.md` draft.5: the exception path (§12.6): `arbiter` class, `exception` entity and events, `exceptable` rules and waivable codes; a grant admits the exact refused request under its original signature.
- `schemas/`: request, envelope, legality table and profile schemas.
- `policy/`: the core legality table with one schema per event type, and the `1-hive` profile.
- `fixtures/`: 10 core and 6 `1-hive` golden fixtures, built by `fixtures/build.py` against the deterministic `fixtures/make-repo.sh` repository.
- `hiverecord`: the fold engine and gate (one rule drives both), hivepin pin admission and policy resolution, the Postgres store (roles, append-only trigger, advisory-lock append, export, restore), the Starlette gateway (signed writes and reads, SSE), the signed client, the `hive` CLI, scratch hives in rootless Podman, and log re-verification.
- Tests: golden replay (in memory and through the real gateway and database), standalone fold, signature re-verification, §13.3 extension refusals, property tests under both profiles and both modes, storage and concurrency guarantees, and a CLI walkthrough.
