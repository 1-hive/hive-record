# Adoption note: 1-hive

1-hive is the first hive running the record for real work. This note says what it took, what worked, what broke, and where the deployment still falls short of the spec. The deployment itself (compose file, registry, actor list, scripts) lives in the [`1-hive`](https://github.com/1-hive/1-hive) repository under `deploy/`.

**Period:** 2026-09-28 to 2026-09-30. **Log at the time of writing:** 138 events, and every signature re-verifies from the export alone (`hive verify-log --registry`), across one policy change.

## Setup

| Choice | 1-hive | Notes |
|---|---|---|
| Profile | `1-hive` | Goals, proposals, leases, supervision and cost. There was no earlier flow to mirror. |
| Mode | `authoritative` from the start | Hives with a running process should start in `mirror` (§20). |
| Policy | `hive-record:policy` at `spec-v1.0`, then `spec-v1.0-a1` | Moved with one operator-signed `hive.policy_changed` (position 61) when amendment A1 (the `route.` family) arrived. |
| Registry | `hive-record` (policy), `workspace` (goals, orders, reports), `mtg-player` (code) | Local bare repositories count as the trusted remotes. No hosting is needed. |
| Postgres | Its own rootless Podman container, loopback only | No WAL archiving and no backups yet (deferred by the operator). |
| Gateway | A systemd user service on `127.0.0.1:8470` | TCP loopback, not a Unix socket: agents run on the host, not in containers (see gaps). |
| Bootstrap | `deploy/up.sh`: idempotent, every step skipped when done | Re-running only restarts the gateway, which records a new `gateway.started`. |
| Reset | `deploy/reset.sh --yes-destroy-the-log` | Archives and verifies the log, then rebuilds. Not needed so far. |

**Actors** (all registered by the operator from public keys): `operator` (the human), `cos` (`chief_of_staff`, a Claude Code session), `worker.claude.1` (`worker`), `reviewer.codex.1` and `reviewer.claude.1` (`reviewer`), `router` (`instrument`, the R8 router) and `supervisor`.

## What ran through it

Three goals, each approved and later accepted by the operator:

| Goal | Tasks | Reviews | Notes |
|---|---|---|---|
| `human-browser-play` | 3 | 3 passed | A browser game against the agents, with an isolated agent seat and a 253-check suite. The operator played an acceptance game. |
| `card-images` | 1 | 1 failed, 1 passed | The first review failed on test independence; the worker was restarted with the review attached and fixed it. |
| `arena-export` | 1 | 1 passed | Port to `xmage-agent-arena` with provenance. |

A fourth goal (`fresh-clone-fixes`) was running at the time of writing, on a self-hosted model chosen by the router.

Every task followed the same cycle: `cos` created it with a pinned order; the worker accepted, planned, checked in and posted a pinned result; `cos` assigned a reviewer to that exact result; the reviewer, on a different model family and signing with its own key, recorded a verdict; and `cos` closed the task only after a pass. The operator's only record events were goal approvals and acceptances, one policy change and two rounds of actor registration.

## What worked

- **Review gating.** No task closed without an independent pass on its current result. The failed review in `card-images` was caught, sent back, fixed and re-reviewed.
- **Signed authorship.** Every event re-verifies from the export. Agents never held the operator's key; `cos` refused to sign as another actor even when that would have been convenient.
- **Refusals as evidence.** Three refusals, each recorded and each informative: a task assigned before it existed (`UNKNOWN_TASK`, a workspace push race), a worker adding an undeclared field (`SCHEMA_INVALID`), and a router test log syncing into the real record (`SCHEMA_INVALID`). None changed state.
- **Corrections by new events.** The router's stray mode change was corrected by a later event rather than erased.
- **Policy evolution.** Amendment A1 went from spec text to a running hive with one signed event, with no downtime.

## What broke, and what changed because of it

1. **A worker stalled on an interactive permission prompt overnight.** Workers now run in non-interactive print mode (`1-hive/tools/launch-task.sh`), where a refused action goes back to the agent instead of waiting.
2. **Print-mode workers that wait on background jobs lose them** when the session ends. The worker contract now has a "Running mode" rule.
3. **Usage limits** (Codex weekly, Claude session) paused work three times. Restarts from the last checkpoint lost nothing. The router (R8) now tracks capacity and passes the failure class along.
4. **Restart context:** fresh task folders lacked 1.7 GB of pinned build artifacts, so the launcher now copies them in.
5. **Standalone verification** of a log that spans a policy change needed every pinned tree. `hive fold` and `hive verify-log` now take `--registry` and materialize pinned trees themselves, and a missing tree is a clean error (hiverecord 1.0.0).
6. **Stale actor declarations:** when the router moved a worker to another model, the actor's declaration went stale. The launcher now posts `actor.declared` with the actor's own key before such a launch.

## Where 1-hive falls short of the spec

- **Key custody (§6.6) is only partly met.** Agents run as the operator's OS user, not in their own sandboxes, so a misbehaving agent could read another actor's key file. Separate keys and the agents' own discipline are the only protection. Fixing this needs a dedicated OS user and per-agent containers (1-hive Phase E).
- **Supervision isn't on the record yet.** Restarts and nudges were done by `cos` with a watch script. The `supervisor` actor exists, but no `task.nudged` or `task.restarted` event has been recorded.
- **No backups.**

## Evidence for promoting extensions into the core

From this log: 6 reviews (1 failed, then passed on rework); the operator made no task-level decisions at all, only goal-level ones; and there were 4 restarts (usage limits and a stuck prompt), all recovered from checkpoints. That's a small sample, but it supports goals as the unit of human approval and review tasks as ordinary tasks. Leases need the supervisor on the record before they can be judged.
