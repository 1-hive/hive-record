# One Hive R1 + R2: The Hive Record — Specification

**Status:** draft v1 for review (not frozen)
**Version:** 1.0-draft.1 · 2026-09-23
**Releases:** R1 (the record), R2 (legality table and review-gated close)
**Depends on:** R0 pinning spec v1.0 (`hivepin` 1.0.0)
**Primary consumers:** the 1-hive operating loop (launcher, supervisor, triage, chief of staff), R3 projections, R5 review harness, R6 worker runtime, R7 messaging, R9 replay.

## 1. Purpose

The record is a hive's single source of truth for **coordination facts**: goals, tasks, who owns what, what was reviewed, what was refused, and who authorized what. It is an append-only event log with exactly one writer, the **gateway**. A declarative **legality table** decides which events are admitted. Every view of the hive (board, inbox, metrics, the Atomspace projection) is computed deterministically from the log.

The record holds events, not content. Content lives in git and is referenced by R0 pins.

## 2. Normative language

**MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT** and **MAY** state requirements.

## 3. Non-goals

This release does not provide: agent launching, supervision or triage behavior (these are Phase E consumers of this spec); scheduling, dependencies or priorities; message transport (R7); skills (R4); routing (R8); signatures or hash chains; multiple writers or high availability; access control on reads beyond authentication; the mirror adapter.

## 4. Terminology

| Term | Meaning |
|---|---|
| Hive | One deployment with one log. Its id matches `^[a-z0-9][a-z0-9._-]{0,63}$`. |
| Log | The hive's append-only, totally ordered sequence of events. |
| Position | An event's gapless ordinal in the log, starting at 1 and assigned by the gateway. |
| Gateway | The only process that writes the log. |
| Actor | A durable identity that emits events: a human, an agent or a program. |
| Class | An actor's authority class (§6.1). The legality table speaks only in classes. |
| Role label | A per-hive free-form label on an actor (e.g. `ai-researcher`). It carries no authority. |
| Request | What a client submits (§7.2). |
| Event | A request admitted by the gateway, with server fields added (§7.1). |
| Refusal | A `gateway.rejected` event recording a request that was not admitted. |
| Fold | The deterministic function from a log prefix to hive state (§12). |
| Legality table | The pinned data file of transition rules (§11). |
| Guard / effect | A rule's admission check / its state change under the fold. |
| Goal | A human-approved objective that scopes agent authority. |
| Task | A unit of work under a goal, with one owner at a time. |
| Current result | The most recent `task.result_posted` event of a task. |
| Proposal | An event another actor asks an authorized approver to apply. |
| Pin | A canonical R0 pin object. |

## 5. Invariants

1. **Append-only.** No event is ever modified or deleted. Corrections are new events.
2. **Single writer.** Only the gateway writes the log. This is a property of database grants, not of convention (§16).
3. **Total order.** Every event has a unique, gapless position. No client clock is trusted.
4. **Authenticated authorship.** Every event's actor is derived from the credential presented, never from the request body.
5. **One specification.** Admission (gate) and state derivation (fold) are computed from the same legality-table rule. An admitted event always changes exactly what its rule says.
6. **Refusals are recorded.** Every refused request from an authenticated actor is itself an event.
7. **Refs, not bulk.** Events carry pins and short structured fields only. Every pin is verified with publication before admission.
8. **Review-gated completion.** A task cannot reach `done` unless the latest review of its current result passed, and that review was recorded by an actor other than the result's author.
9. **Goal-scoped work.** A task can only be created under an `active` goal.
10. **Replayable.** The fold of any log prefix is deterministic and depends only on the log and the legality tables the log pins.

## 6. Identity and authority

### 6.1 Classes (v1, closed)

| Class | Purpose |
|---|---|
| `operator` | A human with final authority: approves, accepts and abandons goals; can do what coordinators do. |
| `chief_of_staff` | The human's front agent: proposes goals, plans, answers questions, closes tasks. |
| `coordinator` | Plans and assigns within active goals. |
| `worker` | Owns and performs tasks. |
| `reviewer` | Records reviews. Can also own review tasks. |
| `supervisor` | Liveness: nudges, restarts, reassigns, escalates. Includes the triage agent. |
| `instrument` | Deterministic emitters (metrics, notifier). No task transitions in v1. |
| `gateway` | The gateway itself. Emits only `hive.initialized` and `gateway.rejected`. |

Each actor has exactly one class. A human who also wants to act as a worker registers a second actor.

### 6.2 Actors

Actor ids match `^[a-z0-9][a-z0-9._-]{0,63}$`. An actor exists from its `actor.registered` event and is `active` until `actor.retired`. A retired actor can't emit anything, and its id is never reused.

An actor's **declaration** records what the actor is: `harness`, `model_route`, `sandbox` (short strings), and optionally a `role_prompt` pin. It is set at registration and replaced by `actor.declared`. Agents MUST re-declare when their configuration changes, including self-modification.

### 6.3 Credentials

- A credential is a 256-bit random secret, base64url-encoded with the prefix `hive1_`.
- It is issued through the API (§17.4), shown once, and recorded only as its fingerprint, `sha256:<hex of SHA-256(secret)>`, in `actor.credential_issued`.
- A credential is valid iff it has been issued, not revoked (`actor.credential_revoked`), and its actor is active. Validity is therefore a function of the fold, and the gateway keeps no separate credential store.
- Clients send it as `Authorization: Bearer hive1_…`.
- The gateway MUST compare fingerprints in constant time and MUST NOT log secrets.

### 6.4 Derivation

The gateway sets the envelope's `actor` = `{id, class}` of the credential's actor. A request body carrying `actor` is ignored for authorship purposes. A request with a missing or invalid credential gets HTTP 401 and is **not** recorded, because it can't be attributed and recording it would let anyone write to the log. Such requests are logged by the service.

### 6.5 Channel attribution

An actor MAY send `X-Hive-Via: <label>` (max 64 chars, `^[a-z0-9][a-z0-9:._-]*$`), e.g. `telegram`, `mcp:chief-of-staff`. The gateway records it as `via`. It is attribution only and carries no authority.

## 7. Events

### 7.1 Envelope (`hive.event/1`)

| Field | Set by | Required | Semantics |
|---|---|---|---|
| `schema` | server | yes | `"hive.event/1"` |
| `hive` | server | yes | Hive id. |
| `position` | server | yes | Gapless integer ≥ 1. |
| `event_id` | server | yes | UUIDv7, lowercase canonical text. |
| `recorded_at` | server | yes | RFC 3339 UTC with microseconds. Informational; order is `position`. |
| `actor` | server | yes | `{id, class}` from the credential. |
| `via` | server | no | From `X-Hive-Via`. |
| `on_proposal` | server | no | The `proposal_id` whose approval applied this event (§13). |
| `verdict` | server | mirror mode only | `{legal: bool, code?, reason?}` (§11.5). |
| `type` | client | yes | Event type (§9). |
| `goal` | client or server | per type | Client-supplied for `goal.*` and `task.created`. Server-filled from the fold for other task events. |
| `task` | client | per type | Task id for task-scoped events. |
| `basis` | client | yes | The highest position the client had observed when deciding. |
| `idempotency_key` | client | yes | 1–128 chars, `^[A-Za-z0-9._:-]+$`. |
| `refs` | client | yes (may be empty) | Array of `{rel, pin}` (§8). At most 16. |
| `data` | client | yes (may be `{}`) | Type-specific fields (§9). |

Ids for goals, tasks, projects and proposals are client-chosen, match `^[a-z0-9][a-z0-9._-]{0,63}$`, and are unique within the hive. Reusing one is refused (`TASK_EXISTS`, `GOAL_EXISTS`, and so on).

### 7.2 Request

A request is a JSON object with exactly: `type`, `basis`, `idempotency_key`, `refs`, `data`, and, when applicable, `goal` and `task`. It may also carry the optional `generation` (§15.3). Unknown top-level fields are refused (`SCHEMA_INVALID`).

### 7.3 Canonical encoding

The canonical bytes of an event are the R0 canonical JSON encoding of the envelope: UTF-8, no insignificant whitespace, keys ordered by code point, no trailing LF. The gateway stores the canonical bytes and `digest` = `sha256:<hex>` of those bytes. Readers MAY verify the digest. Numbers are integers, or decimals with at most 6 fractional digits. Floats are otherwise forbidden, so that encodings are identical across implementations.

### 7.4 Size limits

- The request body is at most 64 KiB.
- `data` canonical bytes are at most 8 KiB.
- Each string field has the `maxLength` stated in its event schema. If none is stated, it is 500.

A request over a limit is refused with `PAYLOAD_TOO_LARGE`.

## 8. References and pin admission

Each `refs` entry is `{"rel": <rel>, "pin": <canonical R0 pin object>}`. Unknown fields are refused.

**v1 rels:** `goal`, `order`, `question`, `answer`, `result`, `review`, `report`, `summary`, `context`, `diagnosis`, `rationale`, `policy`, `role_prompt`.

For every ref, the gateway MUST:
1. check that the rel is required or allowed for the event type by the active legality rule (`REF_MISSING`, `REF_NOT_ALLOWED`);
2. validate the pin against `pin-v1.schema.json` (`PIN_INVALID`);
3. run `hivepin.verify(pin, publication="required")` against the hive's repository registry. On failure the refusal is `PIN_INVALID` with `detail.hivepin_code`; if the remote can't be reached it is `PIN_UNAVAILABLE`, which is `retryable: true`.

The gateway MAY cache successful publication checks per `(repository, commit_oid)`. A cached success MUST NOT outlive the gateway process. Pins MUST NOT be rewritten or normalized: the stored pin is exactly the one submitted.

## 9. Event catalog (v1)

Fields are in `data` unless noted. "pin(rel)" means a ref with that rel. Every event type has a JSON Schema at `schemas/events/<type>.schema.json`, and these schemas are authoritative for field types and lengths.

### 9.1 Hive and projects

| Type | Data | Refs |
|---|---|---|
| `hive.initialized` | `mode` ∈ {authoritative, mirror}; `registry_digest` (sha256 of the repository-registry file); `operator` {id, role, declaration} | pin(policy) |
| `hive.policy_changed` | `reason` | pin(policy) |
| `hive.registry_changed` | `registry_digest`; `reason` | — |
| `hive.mode_changed` | `mode` = `authoritative` | — |
| `project.opened` | `project` (id); `title` | optional pin(summary) |
| `project.closed` | `project`; `reason` | — |

### 9.2 Actors

| Type | Data | Refs |
|---|---|---|
| `actor.registered` | `actor_id`; `class`; `role` (label); `declaration` {harness, model_route, sandbox} | optional pin(role_prompt) |
| `actor.declared` | `actor_id`; `declaration` | optional pin(role_prompt) |
| `actor.retired` | `actor_id`; `reason` | — |
| `actor.credential_issued` | `actor_id`; `fingerprint` | — |
| `actor.credential_revoked` | `actor_id`; `fingerprint`; `reason` | — |

### 9.3 Proposals

| Type | Data | Refs |
|---|---|---|
| `proposal.submitted` | `proposal_id`; `proposed` (a complete request object, §7.2); `approver_class` ∈ {operator, chief_of_staff, coordinator} | optional pin(rationale) |
| `proposal.approved` | `proposal_id` | — |
| `proposal.rejected` | `proposal_id`; `reason` | — |
| `proposal.withdrawn` | `proposal_id`; `reason` | — |

### 9.4 Goals

| Type | Data | Refs |
|---|---|---|
| `goal.proposed` | `project`; `title` (≤200); `objective` (≤1000); `relevance` (≤300, "what remains useless even if all tests pass"); `budget` {usd?, tokens?, wall_clock_seconds?} | pin(goal) |
| `goal.approved` | — | — |
| `goal.revised` | `reason`; optional `title`, `objective`, `relevance`, `budget` | optional pin(goal) |
| `goal.completed` | `outcome` (≤500) | pin(summary) |
| `goal.accepted` | optional `note` | — |
| `goal.reopened` | `reason` | — |
| `goal.abandoned` | `reason` | — |
| `goal.escalated` | `to` ∈ {chief_of_staff, operator}; `code` ∈ {budget_exceeded, at_risk, other}; `reason` | optional pin(diagnosis) |
| `goal.escalation_resolved` | `resolution` | — |

A budget dimension that is absent is unlimited. Spend is computed from the log (§12.3).

### 9.5 Tasks

| Type | Data | Refs |
|---|---|---|
| `task.created` | `title` (≤200); optional `kind` ∈ {work, review} (default `work`); optional `reviews_task` (required iff kind=review) | pin(order) |
| `task.assigned` | `to` (actor id); `lease` {accept_within_seconds, checkin_every_seconds} | — |
| `task.accepted` | — | — |
| `task.declined` | `reason` | — |
| `task.blocked` | `needs` ∈ {decision, information, access, external}; `reason` | pin(question) |
| `task.answered` | optional `note` | pin(answer) |
| `task.unblocked` | — | — |
| `task.reported` | `kind` ∈ {progress, checkpoint, finding, reflection}; optional `cost` | pin(report) |
| `task.result_posted` | optional `cost` | pin(result) |
| `review.recorded` | `verdict` ∈ {passed, failed}; `result_event` (event_id); optional `cost` | pin(review) |
| `task.closed` | optional `note` | — |
| `task.reassigned` | `to`; `lease`; `reason` | optional pin(diagnosis) |
| `task.released` | `reason` | optional pin(report) |
| `task.cancelled` | `reason` | — |
| `task.nudged` | `reason`; optional `message` (≤2000) | optional pin(diagnosis) |
| `task.restarted` | `reason` | pin(context); optional pin(diagnosis) |
| `task.escalated` | `to` ∈ {chief_of_staff, operator}; `code` ∈ {repeated_failure, stuck, review_loop, question, other}; `reason` | optional pin(diagnosis) |
| `task.escalation_resolved` | `resolution` | — |

`cost` = `{usd?, tokens?, wall_clock_seconds?}`: the cost incurred **since the same actor's previous cost report on this task**. Values are non-negative.

### 9.6 Gateway

| Type | Data |
|---|---|
| `gateway.rejected` | `code`; `reason`; `retryable`; `by` {id, class}; `via`?; `refused` (the request if it parsed as a JSON object within the size limit, else null); `refused_digest`; `refused_size`; `detail` {} |

Reserved type prefixes, refused in v1 (`UNKNOWN_EVENT_TYPE`): `message.`, `skill.`, `route.`, `trial.`, `memory.`.

## 10. Lifecycles

### 10.1 Goal

States: `proposed`, `active`, `completed`, `accepted`, `abandoned`. `accepted` and `abandoned` are terminal.

| Event | Classes | From → To | Conditions |
|---|---|---|---|
| `goal.proposed` | chief_of_staff, operator | ∅ → proposed | `project_open` |
| `goal.approved` | operator | proposed → active | — |
| `goal.revised` | chief_of_staff, operator | proposed, active → same | `budget_not_raised_unless_operator` |
| `goal.completed` | chief_of_staff, operator | active → completed | `goal_has_no_open_tasks` |
| `goal.accepted` | operator | completed → accepted | — |
| `goal.reopened` | operator | completed → active | — |
| `goal.abandoned` | operator | proposed, active, completed → abandoned | — |
| `goal.escalated` | supervisor, chief_of_staff | proposed, active, completed → same | — |
| `goal.escalation_resolved` | chief_of_staff, operator | same → same | `goal_is_escalated` |

Abandoning a goal does not change its tasks. The chief of staff is expected to cancel them. The inbox shows abandoned goals that still have open tasks.

### 10.2 Task

States: `created`, `assigned`, `in_progress`, `blocked`, `in_review`, `done`, `cancelled`. `done` and `cancelled` are terminal. "Open" means non-terminal.

| Event | Classes; relation | From → To | Conditions |
|---|---|---|---|
| `task.created` | chief_of_staff, coordinator, operator | ∅ → created | `goal_active`; `review_task_target_valid` |
| `task.assigned` | chief_of_staff, coordinator, operator | created → assigned | `target_active_class(worker, reviewer)`; `review_task_assignee_not_author` |
| `task.accepted` | worker, reviewer; owner | assigned → in_progress | — |
| `task.declined` | worker, reviewer; owner | assigned → created | — |
| `task.blocked` | worker, reviewer; owner | in_progress → blocked | — |
| `task.answered` | chief_of_staff, operator | blocked → blocked | — |
| `task.unblocked` | worker, reviewer; owner | blocked → in_progress | `answered_if_needed` |
| `task.reported` | worker, reviewer; owner | assigned, in_progress, blocked, in_review → same | — |
| `task.result_posted` | worker, reviewer; owner | in_progress → in_review | — |
| `review.recorded` | reviewer, operator | in_review → in_review (passed) · in_progress (failed) | `targets_current_result`; `reviewer_not_author` |
| `task.closed` | chief_of_staff, coordinator, operator | in_review → done | `current_result_passed` |
| `task.reassigned` | chief_of_staff, coordinator, supervisor, operator | assigned, in_progress, blocked → assigned | `target_active_class(worker, reviewer)`; `target_is_not_owner`; `review_task_assignee_not_author` |
| `task.released` | worker, reviewer; owner | assigned, in_progress, blocked → created | — |
| `task.cancelled` | chief_of_staff, operator | open → cancelled | — |
| `task.nudged` | supervisor | assigned, in_progress, blocked, in_review → same | — |
| `task.restarted` | supervisor | assigned, in_progress, blocked → same | — |
| `task.escalated` | supervisor, chief_of_staff, worker, reviewer; owner | open → same | — |
| `task.escalation_resolved` | chief_of_staff, operator | same → same | `task_is_escalated` |

**Review tasks.** A task of `kind: review` reviews another task. Its result pin is the review document, and closing it follows the same rules. Its owner records the verdict on the reviewed task with `review.recorded`. This lets review work itself be assigned, supervised and restarted. A review may also be recorded without a review task, e.g. by an operator.

**Author.** The author of a result is the actor of its `task.result_posted` event. `reviewer_not_author` compares the reviewing actor with that author.

### 10.3 Proposal

States: `pending`, `approved`, `rejected`, `withdrawn`. The last three are terminal.

| Event | Classes; relation | From → To | Conditions |
|---|---|---|---|
| `proposal.submitted` | any active actor except gateway | ∅ → pending | `proposed_request_well_formed` |
| `proposal.approved` | operator, chief_of_staff, coordinator | pending → approved | `approver_matches`; the proposed event is admissible with the approver as actor (§13) |
| `proposal.rejected` | operator, chief_of_staff, coordinator | pending → rejected | `approver_matches` |
| `proposal.withdrawn` | proposer | pending → withdrawn | — |

### 10.4 Hive, project and actor events

| Event | Classes | Conditions |
|---|---|---|
| `hive.initialized` | gateway | `log_empty` |
| `hive.policy_changed`, `hive.registry_changed` | operator | — |
| `hive.mode_changed` | operator | `mode_is_mirror` |
| `project.opened` | chief_of_staff, operator | the project doesn't exist |
| `project.closed` | operator | `project_has_no_open_goals` |
| `actor.registered` | operator | the id is unused; `class_registrable` |
| `actor.declared` | any class except gateway; relation `self` | the actor is active |
| `actor.retired` | operator | the actor is active; `not_last_operator` |
| `actor.credential_issued`, `actor.credential_revoked` | operator (API only, §17.4) | the actor is active (for issuance) |

## 11. The legality table

### 11.1 Format

The table is a JSON document validated by `schemas/legality-table-v1.schema.json`:

```jsonc
{
  "schema": "hive.legality/1",
  "version": "1.0.0",
  "rules": [
    {
      "event": "review.recorded",
      "entity": "task",                       // hive|project|actor|proposal|goal|task
      "classes": ["reviewer", "operator"],
      "relation": null,                       // null | "owner" | "self" | "proposer"
      "from": ["in_review"],                  // null means the entity must not exist yet
      "to": {"by": "verdict", "map": {"passed": "in_review", "failed": "in_progress"}},
      "refs": {"required": ["review"], "allowed": []},
      "conditions": ["targets_current_result", "reviewer_not_author"],
      "effects": ["record_review"]
    }
  ]
}
```

- `to` is a state name, `"same"`, or `{by, map}` (the new state chosen by a `data` field).
- `relation` narrows who within the allowed classes may emit:
  - `owner`: an actor of class `worker` or `reviewer` must be the task's current owner. Other classes aren't affected.
  - `self`: an actor that isn't an `operator` must be the subject (`data.actor_id`).
  - `proposer`: the actor must be the proposal's proposer.
- `conditions` and `effects` are names from the fixed vocabularies below, optionally with arguments: `{"name": "target_active_class", "args": ["worker", "reviewer"]}`.
- The table MUST contain exactly one rule per event type.
- The table MUST NOT name conditions or effects outside the vocabulary. The gateway refuses to start with such a table.

`policy/legality-v1.json` is the normative v1 table. It encodes §10 exactly.

### 11.2 Evaluation (the gate)

For a request `r` from actor `a`, against state `S` = the fold at the current head, the checks run in this order, and the first failure decides the code:

1. Authentication (§6.4), then schema validity of the envelope and data (`SCHEMA_INVALID`, `UNSUPPORTED_SCHEMA`, `PAYLOAD_TOO_LARGE`, `UNKNOWN_EVENT_TYPE`).
2. Generation (§15.3) and idempotency (§15.1).
3. Rule lookup. Class check (`NOT_AUTHORIZED`), then relation check (`NOT_OWNER`).
4. Entity existence: `UNKNOWN_TASK`, `UNKNOWN_GOAL`, `UNKNOWN_PROPOSAL`, `UNKNOWN_PROJECT`, `UNKNOWN_ACTOR`, or the `*_EXISTS` codes.
5. From-state (`ILLEGAL_TRANSITION`).
6. Refs rels (`REF_MISSING`, `REF_NOT_ALLOWED`), then conditions in listed order (each has its own code, §11.3).
7. Pin verification (§8), last, because it touches the network.

If every check passes, the event is appended and its effects are applied by the fold. The whole evaluation and the append MUST happen under the gateway's write lock against the current head.

### 11.3 Conditions vocabulary (v1)

| Condition | Holds when | Code on failure |
|---|---|---|
| `project_open` | `data.project` exists and is open | `PROJECT_NOT_OPEN` |
| `goal_active` | the request's `goal` is `active` | `GOAL_NOT_ACTIVE` |
| `goal_has_no_open_tasks` | no open task under the goal | `GOAL_HAS_OPEN_TASKS` |
| `goal_is_escalated` / `task_is_escalated` | the entity has an open escalation | `NOT_ESCALATED` |
| `budget_not_raised_unless_operator` | the actor is an operator, or no budget dimension increases or becomes unlimited | `BUDGET_RAISE_REQUIRES_OPERATOR` |
| `target_active_class(c…)` | `data.to` is an active actor of one of classes `c` | `INVALID_ASSIGNEE` |
| `target_is_not_owner` | `data.to` ≠ the current owner | `INVALID_ASSIGNEE` |
| `review_task_target_valid` | kind=review ⇒ `data.reviews_task` exists under the same goal and is `in_review` | `INVALID_REVIEW_TARGET` |
| `review_task_assignee_not_author` | kind=review ⇒ the assignee ≠ the author of the reviewed task's current result | `REVIEW_NOT_INDEPENDENT` |
| `answered_if_needed` | the block's `needs` ∈ {access, external}, or a `task.answered` exists since the block | `NOT_ANSWERED` |
| `targets_current_result` | `data.result_event` = the task's current result | `REVIEW_STALE` |
| `reviewer_not_author` | the actor ≠ the author of the current result | `REVIEW_NOT_INDEPENDENT` |
| `current_result_passed` | the latest review of the current result has verdict `passed` | `REVIEW_REQUIRED` |
| `proposed_request_well_formed` | `data.proposed` is a valid request (§7.2) and not itself a proposal | `SCHEMA_INVALID` |
| `approver_matches` | the actor's class = the proposal's `approver_class`, or `operator` | `NOT_AUTHORIZED` |
| `log_empty` | head = 0 | `ILLEGAL_TRANSITION` |
| `mode_is_mirror` | the current mode is `mirror` | `ILLEGAL_TRANSITION` |
| `project_has_no_open_goals` | every goal of the project is terminal | `PROJECT_HAS_OPEN_GOALS` |
| `class_registrable` | `data.class` ≠ `gateway` | `NOT_AUTHORIZED` |
| `not_last_operator` | another active operator exists, or the retired actor isn't an operator | `LAST_OPERATOR` |

### 11.4 Effects vocabulary (v1)

Effects update fold state (§12) in addition to the `to` state. Unless noted, effects read their inputs from the event:

- `set_owner`: owner = `data.to`; also sets the lease and resets the attempt count to 1.
- `clear_owner`.
- `set_lease`.
- `increment_attempt`.
- `record_block`: sets `needs`, the question pin and `answered = false`.
- `record_answer`: sets `answered = true` and the answer pin.
- `clear_block`.
- `set_current_result`: sets the event id, pin and author, and clears the latest review.
- `record_review`: records the verdict against the result event.
- `add_cost`: adds to the task and goal spend.
- `touch_activity`: sets the owner's last activity to this event's position and time.
- `open_escalation` / `close_escalation`.
- `create_entity` / `update_goal_fields`.
- `register_actor` / `update_declaration` / `retire_actor`.
- `add_credential` / `revoke_credential`.
- `set_policy` / `set_registry` / `set_mode`.

Every event by a task's owner on that task implies `touch_activity`. Any event carrying `cost` implies `add_cost`. Reaching a terminal task state implies `close_escalation`.

### 11.5 Modes

- **authoritative:** a failed check refuses the request (§14).
- **mirror:** a request that fails only at steps 3–6 is **appended** with `verdict: {legal: false, code, reason}`. Its `to` state and effects are applied anyway, and the entity is flagged `violations += 1`. Authentication, schema, idempotency and pin failures are still refused.

The mode is set by `hive.initialized`. It can change only from mirror to authoritative, via `hive.mode_changed`. How a mirror adapter maps a source hive's actors is out of scope for v1.

### 11.6 Policy versions

The fold MUST evaluate each event with the table in force at that event's position. That is the table pinned by `hive.initialized` or by the latest earlier `hive.policy_changed`. The gateway materializes pinned tables through `hivepin` and refuses to start if the current one can't be materialized and validated.

## 12. Fold state (normative)

The fold of a prefix yields the state below. The API's state and board views (§17) serialize it exactly. Times are the `recorded_at` values of the relevant events.

### 12.1 Hive

`hive`, `mode`, `policy` (pin), `registry_digest`, `head` (position).

### 12.2 Actors, projects, proposals

- **actor:** `id`, `class`, `role`, `declaration`, `role_prompt` (pin or null), `status` ∈ {active, retired}, `credentials` (the set of active fingerprints), `registered_at`.
- **project:** `id`, `title`, `status` ∈ {open, closed}.
- **proposal:** `id`, `proposer`, `approver_class`, `proposed`, `status`, `submitted_at`, `decided_by`, `decided_at`.

### 12.3 Goal

`id`, `project`, `status`, `title`, `objective`, `relevance`, `budget`, `goal_pin`, `proposed_by`, `approved_at`, `spent` {usd, tokens} (the sum of `cost` over the goal's tasks), `escalation` {to, code, reason, since} or null, `violations`.

Wall-clock spend is `now − approved_at` over the time spent in `active`. It depends on `now`, so it is computed by projections, not stored in the fold.

### 12.4 Task

`id`, `goal`, `kind`, `reviews_task`, `title`, `order` (pin), `status`, `owner`, `lease`, `attempt`, `assigned_at`, `last_activity` {position, at}, `block` {needs, reason, question, answered, answer} or null, `current_result` {event_id, pin, author, at} or null, `latest_review` {event_id, verdict, reviewer, review_pin} for the current result or null, `results_count`, `reviews_failed_count`, `nudges_since_activity`, `restarts`, `reassignments`, `spent` {usd, tokens, wall_clock_seconds}, `escalation`, `violations`, `created_by`, `created_at`.

### 12.5 Determinism

Two conforming folds of the same log with the same pinned tables MUST produce byte-identical canonical JSON state. Sets serialize as sorted arrays. Maps serialize in code-point key order.

## 13. Proposal application

On `proposal.approved` by actor `A`:

1. Check the approval itself: A's class is the proposal's `approver_class` or `operator`, and the proposal is `pending`.
2. Evaluate the proposed request `p` through the full gate (§11.2, steps 3–7) as if `A` had submitted it, against the current head.
3. If `p` is admissible, the gateway appends **two consecutive events atomically**: `proposal.approved`, then the applied event, whose `actor` = A, `on_proposal` = proposal_id, `via` = the approval's via, and `idempotency_key` = `proposal:<proposal_id>`.
4. If `p` isn't admissible, the approval is refused with `PROPOSAL_NOT_APPLICABLE` and `detail.inner_code`. The proposal stays `pending`.

## 14. Refusals

A refused request from an authenticated actor produces exactly one `gateway.rejected` event (actor = gateway) and nothing else. The HTTP response contains the refusal's code and event. There is no coalescing: repeated refusals are repeated events.

**Stable codes (v1):**
`SCHEMA_INVALID`, `UNSUPPORTED_SCHEMA`, `UNKNOWN_EVENT_TYPE`, `PAYLOAD_TOO_LARGE`, `STALE_GENERATION`, `IDEMPOTENCY_CONFLICT`, `NOT_AUTHORIZED`, `NOT_OWNER`, `UNKNOWN_ACTOR`, `ACTOR_RETIRED`, `ACTOR_EXISTS`, `UNKNOWN_PROJECT`, `PROJECT_EXISTS`, `PROJECT_NOT_OPEN`, `PROJECT_HAS_OPEN_GOALS`, `UNKNOWN_GOAL`, `GOAL_EXISTS`, `GOAL_NOT_ACTIVE`, `GOAL_HAS_OPEN_TASKS`, `BUDGET_RAISE_REQUIRES_OPERATOR`, `UNKNOWN_TASK`, `TASK_EXISTS`, `ILLEGAL_TRANSITION`, `INVALID_ASSIGNEE`, `INVALID_REVIEW_TARGET`, `NOT_ANSWERED`, `NOT_ESCALATED`, `REF_MISSING`, `REF_NOT_ALLOWED`, `PIN_INVALID`, `PIN_UNAVAILABLE`, `REVIEW_REQUIRED`, `REVIEW_STALE`, `REVIEW_NOT_INDEPENDENT`, `UNKNOWN_PROPOSAL`, `PROPOSAL_EXISTS`, `PROPOSAL_NOT_PENDING`, `PROPOSAL_NOT_APPLICABLE`, `LAST_OPERATOR`, `INTERNAL_ERROR`.

Only `PIN_UNAVAILABLE` and `INTERNAL_ERROR` are `retryable: true`. `INTERNAL_ERROR` is recorded if at all possible. Clients MUST rely on codes, not on reason text.

## 15. Idempotency, basis, generation

### 15.1 Idempotency

`(actor, idempotency_key)` identifies an accepted event.
- A request whose key matches an accepted event of the same actor, and whose **request digest** (the sha256 of the canonical request) is equal, returns that event with `duplicate: true` and appends nothing.
- A request with the same key but a different digest is refused with `IDEMPOTENCY_CONFLICT`.
- Refused requests don't consume keys.

### 15.2 Basis

`basis` MUST satisfy 0 ≤ basis ≤ head; otherwise the request is refused with `STALE_GENERATION`. It isn't otherwise gated. It records the log state the actor decided from, for replay.

### 15.3 Generation

- The log has a **generation**: an opaque id created at initialization and replaced by the restore procedure.
- Every read response carries it. Requests MAY carry `generation`, and a mismatch is refused with `STALE_GENERATION`.
- Read requests with `after` or `generation` that don't match get HTTP 409 `STALE_GENERATION`, and the client MUST drop its cursor and re-read.

The generation lives outside the log, in a single-row table writable only by the admin role (§16).

## 16. Storage guarantees (Postgres)

- **Roles:**
  - `hive_admin` owns the schema; it is used only by migrations and restore.
  - `hive_gateway`: `INSERT` on `events`, and `SELECT`.
  - `hive_reader`: `SELECT` only.
- No role other than `hive_admin` has `UPDATE`, `DELETE` or `TRUNCATE` on any table. A trigger on `events` MUST raise on `UPDATE` and `DELETE` for every role.
- Default privileges: new tables are readable by `hive_reader` and never automatically writable.
- **`events` table:** `position BIGINT PRIMARY KEY`, `event_id UUID UNIQUE`, `recorded_at`, `type`, `actor_id`, `goal_id`, `task_id`, `idempotency_key`, `request_digest`, `canonical TEXT`, `digest TEXT`, `envelope JSONB`. There is a unique index on `(actor_id, idempotency_key)` for accepted events, which excludes `gateway.rejected`.
- **Gapless positions:** allocated as head + 1 under a transaction-scoped advisory lock held for the whole gate-and-append.
- **Restore:** load a dump as `hive_admin`, then replace the generation. Events after the dump are lost. Operators SHOULD run the log with WAL archiving if that matters.

## 17. HTTP API (v1)

All bodies are JSON (UTF-8). Every response includes `generation` and `head`. Every endpoint requires a valid credential. In v1, every active actor may read everything.

### 17.1 Write

`POST /v1/events`, with a request body (§7.2).
- `201 {"status":"accepted","event":…}` on admission.
- `200 {"status":"duplicate","event":…}` for an idempotent repeat.
- `422 {"status":"refused","code","reason","retryable","refusal":<event>}` for a recorded refusal.
- `401` unauthenticated; `413` over the request limit (also recorded); `409` stale generation (recorded).

### 17.2 Read

- `GET /v1/events?after=<pos>&limit=<≤1000>&type=&goal=&task=` → `{events:[…]}` in position order.
- `GET /v1/subscribe?after=<pos>` → Server-Sent Events, one event per log event (`id:` = position). It resumes with `Last-Event-ID`.
- `GET /v1/state?at=<pos>` → the fold (§12) at `at`, default head.
- `GET /v1/goals/{id}`, `GET /v1/tasks/{id}` → the entity state plus its event history.
- `GET /v1/inbox?for=<operator|chief_of_staff>` → the inbox (§18.2).

### 17.3 Health

`GET /v1/health` (no authentication) → `{"status":"ok","hive","head","generation","policy_version"}`.

### 17.4 Credentials

- `POST /v1/actors/{id}/credentials` (operator) → `201 {"credential":"hive1_…","fingerprint", "event"}`. The secret is returned only here.
- `DELETE /v1/actors/{id}/credentials/{fingerprint}` (operator) → the revocation event.

### 17.5 Bootstrap

`hive init` (run with `hive_admin`) creates the schema, generates the generation id, and has the gateway append `hive.initialized` at position 1, `actor.registered` for the first operator, and `actor.credential_issued`. The first operator credential is printed once. `hive init` refuses a non-empty log.

## 18. Projections

### 18.1 Board

`hive board [--goal] [--project] [--at]` renders open tasks grouped by goal and state, with owner, attempt, last activity and escalation marks. It is a pure rendering of §12.

### 18.2 Inbox

The inbox is a pure function of the fold. It is the source for all human notifications (the slow bus). Items for `operator`:

1. goals in `proposed`: to approve;
2. goals in `completed`: to accept or reopen;
3. pending proposals whose `approver_class` is `operator`;
4. goals and tasks with an open escalation `to: operator`: budget overrun, repeated failure, and the others;
5. abandoned goals with open tasks.

Items for `chief_of_staff`: pending proposals addressed to it, escalations `to: chief_of_staff`, tasks `blocked` and not yet answered, and goals whose tasks are all terminal while the goal is still `active`.

Each item has a stable key, so notifiers can de-duplicate: `(kind, entity id, position of the causing event)`.

### 18.3 Export

`hive export [--after]` writes the log as canonical JSONL, one event per line. `hive fold <file>` computes the state from an export without a database.

## 19. Conformance fixtures

`fixtures/<name>/` contains:
- `log.jsonl`: canonical events;
- `policy/…`: the tables it pins;
- `expected/state@<pos>.json` for selected positions;
- `expected/inbox@<pos>.json`;
- `requests.jsonl` plus `expected/responses.jsonl`, for gate fixtures replayed against a live scratch gateway.

Pins in fixtures reference a fixture git repository created by `fixtures/make-repo.sh`, which must be deterministic.

Required fixtures:
- `happy-path`: goal → task → result → independent review → close → goal accepted;
- `review-loop`: a failed review, a new result, a pass;
- `stale-review`;
- `block-answer`;
- `reassign-restart`;
- `escalations`;
- `proposals`;
- `policy-change`;
- `mirror-mode`;
- `refusals`: one per code in §14 that can be reached from a request.

## 20. Acceptance tests

### 20.1 R1

- Fixtures pass through the real gateway and through the standalone fold with identical state.
- `hive_reader` can't insert; `hive_gateway` can't update or delete; the trigger blocks the owner too.
- Positions are gapless under concurrent writers (a stress test with N parallel clients).
- Idempotent retries under concurrency produce exactly one event.
- A body claiming another actor is recorded under the credential's actor.
- An unauthenticated request leaves the log unchanged.
- A restore changes the generation, and old cursors get `STALE_GENERATION`.
- Pins: unpublished, tampered, unknown-repository and remote-down cases produce the specified codes.
- SSE delivers every event exactly once and resumes correctly.
- A scratch hive (`hive scratch up/down`) leaves no processes, containers or databases behind.

### 20.2 R2: the deliberate-failure suite

For each case, the test asserts: refused with the specified code; a `gateway.rejected` event recorded (when authenticated); state unchanged.

- **Class:** a worker creates, assigns or closes; the chief of staff approves or accepts a goal; the supervisor closes; an instrument makes a task transition; the gateway class can't be registered.
- **Owner:** accept, block, report or post a result on another actor's task.
- **State:** accept unassigned; post a result from `blocked`; close from `in_progress`; any transition out of `done` or `cancelled`; complete a goal with open tasks; create a task under a proposed, completed or abandoned goal.
- **Review:** close without a review; close after a failed review; close after a pass on a superseded result; a review by the author; a review from the worker class; a review task assigned to the author.
- **Budget:** the chief of staff raises or removes a budget dimension.
- **Proposals:** approve twice; approve by the wrong class; approve after state moved on (`PROPOSAL_NOT_APPLICABLE`); nested proposal.
- **Refs:** a missing required rel; a disallowed rel; a malformed, tampered or unpublished pin.
- **Identity:** a retired actor; a revoked credential; retiring the last operator.
- **Structure:** an unknown type; a reserved prefix; an unknown field; oversized data; an idempotency conflict; basis > head.

### 20.3 Properties

Randomized sequences of well-formed requests from random actors, in both modes, check:
- `done` ⇒ the latest review of the current result passed and its reviewer ≠ the result's author;
- every task's goal was `active` at the task's creation;
- in authoritative mode, no appended event failed its guard;
- the fold is deterministic, and the gateway's state = the standalone fold of the export;
- positions are gapless, and each event's digest matches its canonical bytes.

## 21. Deliverables and definition of done

**Deliverables:**
1. `SPEC.md` (this document), frozen as v1.0.
2. `schemas/`: `event-envelope-v1`, `request-v1`, `legality-table-v1`, `events/*.schema.json`.
3. `policy/legality-v1.json`.
4. `fixtures/`.
5. `hivebase`: the gateway, fold, CLI (`hive`) and migrations.
6. The test suites of §20.
7. `docs/OPERATOR_GUIDE.md`.
8. `docs/adoption/1-hive.md`.

**R1 and R2 are done when:**
- all of §20 passes in CI and on a scratch hive of the 1-hive deployment;
- 1-hive runs authoritative for real work;
- the adoption note exists;
- any divergence from this spec is recorded as a v1 amendment.

## 22. Future extensions (not in v1)

These require a new version and must not be improvised into v1:
- message events (R7), skill events (R4), route and canary events (R8), trial events (R9), memory events (R10);
- read access control;
- signed or hash-chained events;
- task dependencies and priorities;
- delegated goal approval;
- mirror actor mapping;
- multiple gateways;
- goal-relevance governor verdicts.

## Appendix A. Changes from PLAN.md

- **One log per hive, with a global position, instead of per-run logs.** Actors are hive-wide and work across projects, so run-scoped logs would force cross-log legality checks. Projects are now a field on goals. Scratch "runs" become scratch hives (separate databases). `run.*` events become `hive.*` and `project.*`.
- **Credentials are validated from the log** (fingerprints in `actor.credential_issued`), so no separate credential store exists.
- **Review tasks** (`kind: review`) let review work be assigned, supervised and restarted like any task.
- **Escalations are explicit events with resolution** (`*.escalated`, `*.escalation_resolved`), so the inbox is a pure fold with no dependence on the current time.
- **`task.answered` is a separate event** so that "answered" is visible before the owner resumes.
