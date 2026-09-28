# hive-record

The hive record for One Hive, releases **R1** (an append-only event log behind a single gateway) and **R2** (a legality table and review-gated close).

- **The record is the single source of truth for coordination facts:** tasks, who owns what, what was reviewed, what was refused, and who authorized what. Content stays in git, referenced by [R0 pins](https://github.com/1-hive/hive-pin).
- **One writer.** Only the gateway writes, and the database enforces it: roles plus an append-only trigger.
- **Signed.** Every request is signed with its actor's own ed25519 key. Signatures are stored with the events, so anyone holding an export can re-verify who asked for what.
- **One rule, two uses.** A pinned legality table drives both the gate (what is admitted) and the fold (what state results), so they can't disagree. A task reaches `done` only after an independent, assigned reviewer passes its current result.
- **Exceptions, on the record.** A move the rules wrongly refuse can be admitted exactly as sent, after a recorded grant by an operator or arbiter. Authority, pins and the review gate are never waived.
- **Core plus profiles.** Every hive runs the core. The `1-hive` profile adds goals, proposals, leases, supervision and cost. Extensions may only add or tighten.

Status: spec **v1.0-draft.5** ([`SPEC.md`](SPEC.md)) with a reference implementation, `hiverecord`. The spec is not frozen yet.

## Layout

| Path | Contents |
|---|---|
| [`SPEC.md`](SPEC.md) | The normative specification: Part I (core), Part II (the `1-hive` profile) |
| [`schemas/`](schemas/) | Request, envelope, legality-table and profile schemas |
| [`policy/`](policy/) | The v1 policy tree: `core/` (legality table, per-event schemas) and `profiles/1-hive/` |
| [`fixtures/`](fixtures/) | Golden conformance fixtures, `make-repo.sh` and `build.py` |
| `src/hiverecord/` | The gateway, fold engine, `hive` CLI and migrations |
| [`docs/OPERATOR_GUIDE.md`](docs/OPERATOR_GUIDE.md) | Running a hive, adoption paths, backups |
| [`docs/adoption/1-hive.md`](docs/adoption/1-hive.md) | How 1-hive adopts it |

## Quick start

```bash
uv tool install "git+https://github.com/1-hive/hive-record"
hive keygen --out ~/.config/hive/me.key            # prints your public key
hive scratch up                                      # a throwaway Postgres (rootless Podman)
```

Then follow the [operator guide](docs/OPERATOR_GUIDE.md): `hive db-bootstrap`, `hive init`, `hive gateway`, `hive emit`, `hive board`.

Without a database, you can fold and verify any exported log:

```bash
hive fold fixtures/core/happy-path/log.jsonl --policy /tmp/hive-record-fixture/fixture/policy --board
hive verify-log fixtures/1-hive/proposals/log.jsonl --policy /tmp/hive-record-fixture/fixture/policy
```

(`fixtures/make-repo.sh` creates `/tmp/hive-record-fixture`.)

## Development

```bash
uv venv && uv pip install -e ".[test]"
uv run pytest                    # everything; the Postgres tests need rootless Podman
uv run pytest -m "not pg"        # without Postgres
python fixtures/build.py         # regenerate the golden fixtures after a policy change
```

For other implementations, the portable contract is `SPEC.md`, `schemas/`, `policy/` and `fixtures/`. A conforming gateway replays every `fixtures/*/*/requests.jsonl` into a byte-identical `log.jsonl` (§21).

Related: [hive-pin](https://github.com/1-hive/hive-pin) (R0, pins), [1-hive](https://github.com/1-hive/1-hive) (plan and deployment), [hive-route](https://github.com/1-hive/hive-route) (R8).

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
