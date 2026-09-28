# Adoption note: 1-hive

How our own hive, **1-hive**, adopts the record. It also serves as an example for other hives. The deployment itself (compose files, registry, actor list) lives in the [`1-hive`](https://github.com/1-hive/1-hive) repository; this note records the choices.

## Choices

| Choice | 1-hive | Why |
|---|---|---|
| Profile | `1-hive` from day one | Goals, proposals, leases and cost are how we intend to run; there is no earlier flow to mirror. |
| Mode | `authoritative` | Same reason: no existing flow to shadow. Other hives with a running process should start in `mirror` (§20). |
| Policy pin | `hive-record:policy` at a published tag | The canonical tree, unmodified. |
| Registry | `hive-record` (policy), `hive-workspace` (orders, results, reviews), `1-hive` (goal documents) | Every pin must resolve through the registry. |
| Postgres | Rootless Podman, loopback only, WAL archiving on | §18; a restore loses events after the export. |
| Gateway | A user service, Unix socket, for this host's sandboxes | Sandboxes get the socket, never database credentials. |

## Actors

| Actor | Class | Sandbox |
|---|---|---|
| the human | `operator` | their own machine; key on their device |
| `cos` | `chief_of_staff` | its own container (Claude Code session with the `hive` MCP server) |
| `sup`, `triage` | `supervisor` | one container each |
| workers | `worker` | one container per worker (launcher, Phase E) |
| reviewers | `reviewer` | one container each, on a different harness or model than the worker |

Each container generates its key with `hive keygen` at first start and prints only the public key; the operator registers it. No container ever mounts another's key (§6.6). The Phase E launcher's acceptance test must show that a worker container has no reviewer key to sign with.

## Steps

1. `hive db-bootstrap` on the Podman Postgres; secrets go to `~/.config/hive/` (git-ignored, outside every repository).
2. The operator runs `hive keygen` on their own device.
3. `hive-pin mint hive-record policy` at the release tag, then `hive init --profile 1-hive --mode authoritative`.
4. Start the gateway service; check `hive health`.
5. Register `cos`, `sup`, `triage` and the first workers and reviewers from the public keys their containers print.
6. `cos` opens the `mtg-player` project and proposes the first goal; the operator approves it from the inbox.
7. First acceptance run, on a scratch hive, then for real: goal → task → independent review → close → goal completed → accepted (the `1-hive/goal-happy-path` fixture is the script).

## What 1-hive learns for One Hive

These extension mechanisms are candidates for the core once they have evidence (§13.3, promotion): review tasks, leases and supervision events, proposals, and goal-level budgets. The evidence to collect, all computable from the log: approval statistics per actor and per proposal kind (`1-hive/PLAN.md` D13), review loop counts, and supervisor interventions per task.
