# hive-record — plan for One Hive R1 + R2, and 1-hive's operating loop

**Status:** agreed plan · 2026-09-23 · rev 6 (signed-key identity, per-worker sandboxes, review assignment; after reviewing Ben's and Kabir's proposals)
**Scope:** R1 (the record) and R2 (legality table and review-gated close) from the *One Hive incremental release plan*, then a minimal operating loop so that our own hive, **1-hive**, actually runs. 1-hive's first workload is `mtg-player` (`~/repos/hive-workspace.git`); more follow.
**Sources, newest-governing first:** the incremental release plan; *One Hive roadmap*, Ben's revision; *roadmap differences* (Aug 31); Cassio's original roadmap; *OmegaHive: A Running Hive* (Jul 29), used as the technical reference for record semantics; the frozen R0 spec and `hivepin` 1.0.0 (Sep 8); and `~/cassio-hive`, a running implementation that we mine for lessons.

---

## 0. The short version

We build the hive's **system of record**:
- **Log.** An append-only event log in Postgres.
- **Gateway.** A single service, the only thing that can write to the log. It identifies callers by credential.
- **Legality table.** A declarative data file saying who may make which change, from which state. The gateway uses it to accept or refuse writes (the *gate*). The board is computed from the log with the same table (the *fold*), so the two can never disagree.
- **Refusals.** Every refused write is itself recorded as an event.
- **Pins.** Every reference to content is an R0 pin, verified when the event is admitted.
- **Review-gated close.** A task can't be closed unless an independent reviewer has passed its current result.

On top of that, 1-hive runs a small operating loop (Phase E):
- **Goals.** The human approves **goals**, not steps. Agents do all the task-level work under an approved goal.
- **Chief of staff.** One front agent is the human's only interface. It starts as a long-running Claude Code session.
- **Supervisor.** A mechanical program keeps tasks moving: it nudges, restarts with context rebuilt from the record, reassigns and escalates.
- **What reaches the human.** Only important things, chosen by fixed rules, not by agents.

The record is **modular, like R0**:
- The **core profile** holds only what the One Hive roadmap agreed. Any existing hive can adopt it alone: first in mirror mode, then authoritative.
- 1-hive's operating model is the **`1-hive` profile**, an extension that may only add or tighten, never loosen.
- Each Phase E piece is its own repo, talking to the record over HTTP.

---

## 1. What R1 and R2 are, in the governing documents

The release plan specifies:

- **R1:** "append-only event log behind a single gateway, minimal event schema, a fold CLI that renders board state, and a throwaway mirror-mode adapter".
- **R2:** "the gateway starts refusing illegal transitions and closes without a recorded passed review; refusals are logged; ships with the deliberate-failure test suite".
- **Frozen in week one:** the pin format (already done in R0), the event schema including its version field, and the role and state vocabulary of the legality table.
- **Consumers to protect:** R3 (projections, OmegaClaw attaching read-only), R5 (review verdicts as events), R6 (worker runtime), R7 (logged messages), R8 (canaries), R9 (replay, shadow trials), and mirror adapters.
- **Done means adopted:** a real hive runs it and files an adoption note.

These are the M1 acceptance clauses R1 + R2 retire:

| # | Clause | Covered by |
|---|---|---|
| 2 | results committed with pinned references | pin verification at admission |
| 4 | done only through the legality table: report → review passed → close; a close without a passing review is refused | R2 |
| 5 | deliberate failure modes are rejected and recorded | R2 test suite |
| 6 | every task-state change is a gateway event by an authorized actor; nothing writes around the gateway | R1 (database roles plus credentials) |
| 8 | tear down clean; scratch record is disposable | R1 scratch runs |

Phase E also touches clauses 1 and 3 (launch with context supplied by the runtime; independent review), but only in their minimal 1-hive form. The full versions are R5 and R6.

Out of scope for R1/R2 themselves: scheduling, dashboards, dependencies and priorities, skills, routing, and any OmegaClaw write path.

---

## 2. How the ideas evolved, and what that means for us

| # | Final-form idea (source) | Consequence |
|---|---|---|
| E1 | The record is the authority for **task state**; agents can keep continuous context and private memory (Ben's revision, invariants 4–5) | The legality table governs task and goal state. Agent memory stays outside the record. Other event families go through the gateway under authority rules only. |
| E2 | Continuous context is included by default; residents are legitimate (revision §2.4, M2–M4) | **Actors are durable identities**, not one session per task. A task is assigned to an actor, and restarts and reassignments are normal. |
| E3 | The 7-role catalog is replaced by per-hive roles composed from skills (revision §2.4) | Fixed **authority classes** (operator, chief_of_staff, coordinator, worker, reviewer, supervisor, instrument, gateway) are separate from per-hive **role labels**. The legality table only talks about classes. |
| E4 | Agents self-modify; regression tests are shared infrastructure (revision §2.3, M3) | Actors **declare** their configuration (harness, model route, role-prompt pin, sandbox) and re-declare whenever it changes. |
| E5 | Replay reconstructs the **recorded** context (revision §2.1) | We record the order pin, the actor's declaration, the log position each decision was made from, the exact result each review judges, and **the pinned context bundle of each restart**. |
| E6 | Two communication tiers; back-end agent messages are logged (R7) | Event families are versioned and extensible. `message.*` is reserved. |
| E7 | Writable shared memory complements the record (R10); the Atomspace view is a read-only projection (R3) | A stable read API plus golden fixtures, so projections can be built correctly. |
| E8 | OmegaClaw is first-class; it wakes on chat or a timer; clients are a mix of short- and long-lived processes | The protocol is language-neutral (HTTP/JSON). |
| E9 | Adopt by running beside a hive first (mirror), then flip to authoritative | Each run has a mode. 1-hive runs authoritative. |
| E10 | R0 is frozen: canonical JSON pins; verification offline isn't enough to admit an event | The gateway verifies pins online with `hivepin`. |
| E11 | Review is mandatory and done by an actor with authority; M1 wants it from a second harness or model | The reviewer class records reviews, the reviewer must not be the author, and a review binds to one exact result. |
| E12 | The strategic-relevance gate is a one-line field (App. A) | It sits on the **goal**, as a required relevance line. Tasks inherit it. |
| E13 | Typed blocked states (M1 road) | `task.blocked` carries `needs` from a closed list. |
| E14 | Hash chain cut unless the record crosses a trust boundary | No chain, but canonical event bytes plus a digest are stored. |
| E15 | The control plane is "ordinary files, schemas, databases, and narrow services" | Table and schemas are pinned data files; the gateway is a narrow service. |
| E16 | Rule 1 (value first), rule 4 (unblock, don't perfect), rule 5 (layered docs) | A deferred list with triggers (§9), and three documentation layers (§10). |
| E17 | Tracked cost per closed finding | Goals carry a budget, and workers report cost (D12). |
| E18 | Two-tier communication with **mechanical promotion rules**, not agent judgment (OmegaHive1 §2.2) | One front agent; fixed rules decide what reaches the human (D15). |
| E19 | Governor: "proposes … never applies; a policy gate applies or rejects; a receipt records requested versus applied". Seats in rehearsal: full function, zero authority (Cassio) | Proposals and approvals are native to the record (D13). |
| E20 | "Deterministic guards and human judgment now" (§2.4, Psyche row). Evidence from Ben's hive: agents crash, get lost, and don't report or finish | **Deterministic detection** plus an **LLM triage agent at triggers only** (D14). Cassio's experiment showed free-running LLM supervisors over-intervene. This documented real-world issue is the rule-1 justification for building it now. |

---

## 3. Design decisions

**Agreed so far (2026-09-23):** gateway as a service (D1); mirror mode built, adapter deferred (D4); approvals at goal level (D12); budgets in any of three units (D12); reviewer ≠ author is enough for v1 (D3); supervisor defaults and triage agent (D14); a single front agent that starts as a Claude Code session (D15); Phase E is in scope; repeated failures interrupt the human; naming (D11); core + profiles and the repo split (D16).

**D1. The gateway is a narrow service with sole write access to the database.** It speaks HTTP/JSON over a Unix socket or loopback/tailnet. Every request is **signed with the actor's own key** (ed25519 in v1; the key type is pluggable), and the gateway **derives actor and class from the key**. Signatures are stored with events, so the log can be re-verified by anyone, including other hives. (Draft.2 used bearer tokens; changed to match the Omega architecture's key-based identity.) This makes "nothing writes around the gateway" and "authorized actor" (clause 6) real, and it lets non-Python and sandboxed clients take part without database credentials.

**D2. Postgres, append-only by construction.**
- Two database roles: `hive_gateway` with `INSERT` only, `hive_reader` with `SELECT` only.
- No `UPDATE` or `DELETE` grants on the log, plus a trigger that raises on either.
- Positions are gapless per run and allocated under a per-run advisory lock. Server wall time is informational.
- A generation token is bumped on restore, so stale cursors are refused.

**D3. One legality table, and one interpreter for both gate and fold.**
- Each rule declares: event type, allowed classes, actor relation (e.g. `owner`), from-states and to-state, required ref kinds, and conditions from a small fixed vocabulary coded in the interpreter.
- The gate evaluates the guard and the fold applies the effect, both from the same rule, so they agree by construction.
- Anything stateful without a rule is refused by default.
- The table is pinned by the run; a policy change is a recorded event.

**D4. Runs have a mode.** `authoritative`: a failed guard is refused. `mirror`: the event is appended with its verdict and the task is flagged. Mirror → authoritative is a one-way, recorded flip. The mirror adapter is deferred until a hive wants to adopt that way.

**D5. Refusals are events.** An authenticated actor's refusal is recorded with the full refused request, a code, a reason and a retryable flag. There is no coalescing by mutation. Unauthenticated requests go to the service log, not the record.

**D6. Pins are verified at admission** with `hivepin.verify(pin, publication="required")` against the run's pinned registry. An unreachable remote gives a retryable refusal.

**D7. Idempotency and basis.** A client key is unique per (run, actor): same key and same content returns the original; different content is refused. `basis`, the last position the client saw, is recorded for replay.

**D8. The schemas are the source of truth.** The gateway enforces the published JSON Schemas directly. Stored events use canonical JSON plus SHA-256.

**D9. Actors and declarations.** Events: `actor.registered`, `actor.declared` (a configuration change or self-modification), `actor.retired`. Only credential fingerprints are recorded, never secrets.

**D10. The read surface is part of R1:** cursor reads, subscription (SSE), the board at any position, task history, goal status, the human's **inbox**, and the run list. The CLI is a thin client. The fold also runs standalone over an exported log.

**D11. Stack and naming.** Python ≥3.11, Postgres, `hivepin`, `psycopg`, `jsonschema`, and a small HTTP layer. Package `hiverecord`, CLI `hive`, repository `github.com/1-hive/hive-record`. Packaged like `hivepin` (hatchling, ruff, pytest, GPL-3.0-or-later). Rootless Podman on this host.

**D12. Goals are the unit of human approval.**
- A goal carries an objective, success criteria, a **relevance line** ("what remains useless even if all tests pass"), a budget and a goal-definition pin.
- Only an **operator** may approve, accept or abandon a goal.
- Under an **active** goal, the chief of staff and coordinator may create, assign and close tasks with no human approval. Creating a task needs an active goal.
- Task close needs an independent, agent-level passed review. In v1, independent means the reviewer isn't the author; Phase E still dispatches reviews to a different harness or model. **The human's review happens at goal level:** accepting or rejecting the completed goal.
- **Budget:** any of `usd`, `tokens` and `wall_clock_seconds`. A dimension that isn't set isn't enforced.
- **Cost:** measured by the model gateway/router, where the agent can't change it, and attached as optional `cost` fields (the same three dimensions) on `task.reported` and `task.result_posted`. Harness self-reports (or AgentsView) are only a labeled estimate, used where the router can't measure, e.g. some subscription usage. Spend per goal is computed from the log. When a set dimension is exceeded, the goal escalates to the human as a budget overrun.

**D13. Proposals and approvals are native to the record.**
- Any agent may submit `proposal.submitted` under its own identity, carrying the exact event it wants recorded.
- The approver's `proposal.approved` is re-checked against the legality table at approval time, then applied with the approver as the authorizing actor and a link back to the proposal.
- In v1 this is mainly how goals are approved. It is also the path to delegating authority later: approval statistics per actor and per proposal kind are the evidence M3/M4 need.

**D14. Leases and a deterministic supervisor.**
- `task.assigned` carries a lease: `accept_within` and `checkin_every`.
- The supervisor reads the record, git and each agent's process state (through its harness adapter). Every action it takes is recorded, so the board stays computable from the log. It works up a ladder:
  1. not accepted in time → reassign;
  2. silent past check-in → check whether the process is alive; nudge if alive, restart if dead;
  3. no progress after a nudge → a fresh attempt;
  4. N failed attempts → reassign to a different agent or model;
  5. still failing, or over budget → escalate to the chief of staff, and to the human if the goal is at risk or the task **fails repeatedly**.
- **Default thresholds**, overridable per goal: check in every 15 min; 2 nudges before a restart; 3 attempts per actor; 2 reassignments before escalating.
- **Judgment goes to a triage agent, invoked only at triggers:** alive but not progressing; repeated failures; a failed-review loop; a state the ladder doesn't cover. It is a fresh LLM session per incident (a supervisor-class actor with its own credential). It reads the task's record plus the agent's transcript and picks one action from a **fixed menu**:
  - nudge with a specific message;
  - restart with added guidance;
  - reassign to a different agent or model;
  - propose a split or re-plan to the chief of staff;
  - escalate.

  The chosen action is recorded as the usual supervision event, with a pinned `diagnosis`. This is the governor pattern (propose from a fixed set of verdicts; a gate applies it) applied to operations. The chief of staff is not the triage agent: that would flood the human's conversation with fast-bus noise. It only receives escalations that affect the plan or a goal.
- **Progress and cost signals:** trial [AgentsView](https://github.com/kenn-io/agentsview) (MIT, local-first; reads Claude Code, Codex and 60+ other harnesses' session files; REST API, MCP server, SSE). It provides last activity, idle time and token/dollar cost for the adapters, and transcripts for triage. It is never authoritative, since transcripts are agents' private context. OmegaClaw needs its own adapter.
- **Restart context is generated from the record**, never written by hand: the order at its latest version, the goal, previous attempts, the last checkpoint, Q&A and review feedback. It is committed, and pinned on `task.restarted`.
- Workers must checkpoint (a `task.reported` of kind `checkpoint`) at least once per check-in interval.

**D15. The interaction model: one front agent, and two buses.**
- The human talks only to the **chief of staff**, a long-running Claude Code session with an MCP server over the gateway. It may later be compared against an OmegaClaw version as an M3 trial.
- The chief of staff proposes goals, answers workers' questions when the goal's context settles them, re-plans on escalation, and delegates **only through the record**.
- **Fast bus:** all agent activity; logged, queryable, never pushed to the human.
- **Slow bus:** an inbox computed from the log by fixed rules.
  - **Interrupts:** a goal to approve; a completed goal to accept; a budget overrun; a task failing repeatedly; an escalation the chief of staff can't resolve.
  - **Digest:** periodic progress per goal.
  - Everything else is silent but can be asked about. A missing digest at its scheduled time is itself an alarm.

**D16. Core + profiles, and one repo per adoptable piece.**
- The goal: other hives can adopt each piece on its own, as with R0.
- **Why:** the roadmap's rule 3 says mechanisms become shared defaults only after beating a baseline. Our goals, proposals, leases and supervision haven't been compared yet, so they're an extension, not the core.
- **Rule:** an extension may add classes, events, rels, codes and conditions, and add extension-only effects and `data.ext` fields. It may never change a core rule's states or relation, grant existing core classes more, or drop conditions. The gateway refuses to load an extension that tries.
- **Promotion:** extensions that prove themselves in 1-hive can be proposed for the next core version.
- **Repos:**

| Repo | Contents | Adoptable alone |
|---|---|---|
| `hive-pin` | R0 | yes |
| `hive-record` (this repo) | R1+R2: spec, gateway, storage, fold engine, CLI, `core` and `1-hive` policies | yes |
| `hive-supervisor`, `hive-launcher`, `hive-cos`, `hive-notify` | Phase E pieces, HTTP clients of the record | yes |
| `1-hive` | our deployment: compose files, pinned policy, registry, actors | no (it's our instance) |

---

## 4. The contracts

The contracts now live in [`SPEC.md`](SPEC.md) and are no longer duplicated here: envelope, catalog, lifecycles, legality table, profiles, refusal codes, API, fixtures and acceptance tests. Part I is the **core**, which every adopting hive runs. Part II is the **`1-hive` profile**: goals, proposals, leases, supervision, cost.

SPEC.md Appendix A lists where the spec departs from this plan's earlier drafts, e.g. one log per hive instead of per-run logs.

---

## 5. What we take from `cassio-hive`, and what we don't

| Take (the idea, rewritten to fit) | Leave, and why |
|---|---|
| Two database roles; tables readable by default, never writable by default (migration 0003 is close to liftable) | Coalescing refusals with `UPDATE (payload)`: breaks append-only |
| Per-run advisory lock around fold, check and append | Guards and effects as separate callables plus an agreement test (D3 replaces it) |
| Generation token on restore | The `path@sha` regex with abbreviated SHAs (superseded by R0) |
| Refusals as events; default-deny | Pydantic models kept apart from the published schema (drift) |
| Idempotency unique index | Dependencies, joins, prune, priority (project management) |
| Server-side time; a scratch database per test | Library-only gateway; dual simulation/server clock |
| The tmux targeting lesson (`=session:{end}`) and the launch-and-recovery experience | Full refold per emit *as a design* (we start with it, and optimize only once measured) |
| Lesson: remove a configuration surface rather than guard it | — |

---

## 6. The deliberate-failure suite (R2)

Specified in SPEC.md §22.2 (core) and §27 (`1-hive`). The core property tests run under every shipped profile, which proves that extensions don't weaken core invariants.

---

## 7. Build sequence

Ordered by dependency and quality gates. No calendar estimates.

**Phase A — Contracts.**
1. `SPEC.md` (normative).
2. Schemas: the envelope, one per event type, and the legality table.
3. `policy/legality-v1.json`.
4. Golden fixtures.
5. Interface sketches for Phase E, so the schema is checked against its real consumers: the harness adapter (`probe`, `nudge`, `stop`, `start(context)`), the context-bundle format, and the inbox rules.
6. **Exit:** reviewed together and frozen as v1.

**Phase B — Record core (R1).**
1. Migrations, database roles and the append-only trigger.
2. Canonical encoding.
3. The gateway: authentication, schema, pin admission, idempotency, positions, refusals, proposal application.
4. The read API, subscription and inbox.
5. The admin CLI.
6. The fold interpreter and mirror-mode gate.
7. `hive board`, `hive events`, `hive task`, `hive goal`.
8. The scratch harness.
9. **Exit:** the fixtures pass through the real gateway, and teardown is clean.

**Phase C — Enforcement (R2).**
1. The authoritative gate.
2. Goal, review and proposal conditions.
3. The failure suite and property tests.
4. The operator guide.
5. **Exit:** the whole suite is green.

**Phase D — 1-hive deployment.**
1. Rootless Podman under a dedicated user; Postgres loopback-only; the gateway as a user service; secrets outside the repositories.
2. Register the `mtg-player` workspace in the `hivepin` registry; open the run with a pinned policy; register the actors.
3. Backups and a restore drill.
4. **Exit:** an end-to-end manual goal → task → review → close on scratch, then on the real run.

**Phase E — The operating loop (minimal, for 1-hive).**
Each piece below is its own repo (D16) and depends only on the record's HTTP API and schemas.

1. **Launcher:** starts a worker for an assigned task with a context bundle generated from the record; each worker gets a task-scoped clone. **Each agent runs in its own rootless Podman container** with its own filesystem and its own key; no two agents share a process, container, filesystem, OS user or key (SPEC §6.6). The launcher starts, watches and stops containers; it never hosts workers itself. First adapter: Claude Code (headless, in a container). Codex as the second harness, for reviews. OmegaClaw adapter next. Every launch carries a stable intent id, and a launch whose outcome is unknown after a crash is marked indeterminate and reconciled, never blindly re-launched.
2. **Review dispatch:** on `task.result_posted`, the chief of staff (or coordinator) emits `review.assigned` and the launcher starts that reviewer in **its own container, with its own key**, on a different harness or model. The verdict goes straight to the gateway, never back through the worker. A failed or `needs_information` review restarts the owner with the review attached.
3. **Supervisor:** a deterministic loop running roughly every minute, using the ladder in D14, plus the triage agent at triggers. Trial AgentsView as the progress and transcript source (cost comes from the router).
4. **Chief of staff:** a long-running Claude Code session with a `hive` MCP server (read, propose, answer, re-plan), in its own container and holding only its own key.
5. **Human surfaces:** the chief-of-staff conversation, and push notifications for interrupts with one-tap approve and accept. Telegram is the candidate; it's formally R7, pulled forward for 1-hive.
6. **Exit:** the adoption criteria below.

Phase A is the gate. B and C overlap: test cases can be written against the spec while B is built. E starts once C's goal and review rules are in place.

---

## 8. Definition of done

- **R1:** spec, schemas and fixtures frozen as v1; the gateway is the only writer, as a database property; pins are verified; the board is reproducible by an independent reader; 1-hive runs it for real work; an adoption note exists (which also closes R0's item 4).
- **R2:** 1-hive is authoritative; the failure suite passes in CI and on a scratch run of the deployment; `done` is unreachable without an independent pass of the current result.
- **1-hive loop (Phase E):**
  - `mtg-player` goals run end to end with the human doing only goal approval and acceptance;
  - at least one crash and one lost agent are recovered by the supervisor without human involvement, as shown by the record;
  - the human receives only the interrupts listed in D15, plus the digest.

---

## 9. Deliberately deferred (rule 1), each with its trigger

| Deferred | Trigger |
|---|---|
| Mirror adapter | Another hive wants to adopt by mirroring |
| Hash chain or signatures | Records cross a trust boundary |
| Refusal flood control | A documented flood |
| Incremental fold | Emit latency measured as a problem |
| High availability, multiple gateways | Measured load |
| Task dependencies and priorities | The chief of staff's planning needs them in practice |
| Delegating goal approval or acceptance | Approval evidence argues for it (M4) |
| Model-diversity requirement for reviewers | R5, or a same-model review that let something slip |
| Triage authority beyond its fixed menu; the governor's goal-relevance verdicts | An M3 trial wins it |

---

## 10. Documentation layers (rule 5)

1. `README.md`: a short overview, written by you.
2. `SPEC.md`: normative, written by the LLM and reviewed closely.
3. Operator guide, design notes and decision log: lightly reviewed.

This plan is replaced by those three.

---

## 11. Open questions

None at the moment. All earlier questions are resolved in §3. New ones get added here as Phase A raises them.
