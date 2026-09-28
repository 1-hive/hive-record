# One Hive R1 + R2: The Hive Record — Specification

**Status:** draft v1 for review (not frozen)
**Version:** 1.0-draft.5 · 2026-09-28
**Releases:** R1 (the record), R2 (legality table and review-gated close)
**Depends on:** R0 pinning spec v1.0 (`hivepin` 1.0.0)
**Primary consumers:** any hive adopting the record; the 1-hive operating loop (launcher, supervisor, triage, chief of staff); R3 projections; R5 review harness; R6 worker runtime; R7 messaging; R9 replay.

This document has two parts:
- **Part I, the core.** Normative for every hive. It contains only what the One Hive roadmap has agreed: the record invariants, the M1 task lifecycle, and the exception path for cases the rules get wrong (§12.6).
- **Part II, the `1-hive` profile.** An extension that adds goals, proposals, leases, supervision and cost. A hive opts into it by pinning it. It is an experiment in the roadmap's sense: parts of it may be proposed for the core once evidence supports them.

---

# Part I — Core

## 1. Purpose

The record is a hive's single source of truth for **coordination facts**: tasks, who owns what, what was reviewed, what was refused, and who authorized what. It is an append-only event log with exactly one writer, the **gateway**.

A pinned **policy** decides which events are admitted. It consists of an event catalog, schemas and a legality table. Every view of the hive (board, inbox, metrics, the Atomspace projection) is computed deterministically from the log.

The record holds events, not content. Content lives in git and is referenced by R0 pins.

**Mechanism vs. policy.** The gateway and the fold engine know nothing about any particular lifecycle: they execute whatever policy the hive pins. The core policy (§11) and the extension rules (§13) keep every hive's record trustworthy regardless of profile.

## 2. Normative language

**MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT** and **MAY** state requirements.

## 3. Non-goals

Not provided by this release:
- agent launching, supervision or triage behavior (consumers of this spec);
- scheduling, dependencies or priorities;
- message transport (R7), skills (R4) or routing (R8);
- hash chains;
- multiple writers or high availability;
- read access control beyond authentication;
- the mirror adapter.

## 4. Terminology

| Term | Meaning |
|---|---|
| Hive | One deployment with one log. Its id matches `^[a-z0-9][a-z0-9._-]{0,63}$`. |
| Log | The hive's append-only, totally ordered sequence of events. |
| Position | An event's gapless ordinal in the log, starting at 1 and assigned by the gateway. |
| Gateway | The only process that writes the log. |
| Actor | A durable identity that emits events: a human, an agent or a program. |
| Class | An actor's authority class. Legality rules speak only in classes. |
| Role label | A per-hive free-form label on an actor (e.g. `ai-researcher`). It carries no authority. |
| Request | What a client submits (§7.2). |
| Event | A request admitted by the gateway, with server fields added (§7.1). |
| Refusal | A `gateway.rejected` event recording a request that was not admitted. |
| Policy | The pinned tree of catalog, schemas and legality tables a hive runs (§10). |
| Profile | The core policy alone, or the core composed with one extension (§13). |
| Fold | The deterministic function from a log prefix to hive state (§14). |
| Guard / effect | A rule's admission check / its state change under the fold. |
| Current result | A task's most recent `task.result_posted` event. |
| Author | The actor of a result's `task.result_posted` event. |
| Key | An actor's public key. Requests are signed with the matching private key (§6.3). |
| Revision | A per-task counter, incremented by every accepted event on that task (§17.4). |
| Pin | A canonical R0 pin object. |
| Exception | A recorded decision, by an operator or arbiter, to admit one specific refused request despite one specific rule (§12.6). |
| Waivable code | A refusal code that an exception may waive. Everything else can never be waived. |

## 5. Core invariants

These hold under every profile. Extensions cannot weaken them (§13.3).

1. **Append-only.** No event is ever modified or deleted. Corrections are new events.
2. **Single writer.** Only the gateway writes the log. This is a property of database grants (§18).
3. **Total order.** Every event has a unique, gapless position. No client clock is trusted.
4. **Authenticated authorship.** An event's actor is the owner of the key that signed the request, never a name in the request body. The signature is stored with the event, so anyone can re-check who asked for it.
5. **One specification.** Admission (gate) and state derivation (fold) come from the same rule, so an admitted event changes exactly what its rule says.
6. **Refusals are recorded.** Every refused request from an authenticated actor is itself an event.
7. **Refs, not bulk.** Events carry pins and short structured fields. Every pin is verified with publication before admission.
8. **Review-gated completion.** A task reaches `done` only if the latest review of its current result passed. That review was recorded either by the reviewer that a coordinator or operator assigned to that result, or by an operator. In both cases the reviewer isn't the result's author, and the verdict goes to the gateway directly, never through the worker.
9. **Replayable.** The fold of any log prefix is deterministic and depends only on the log and the policy trees it pins.
10. **Exceptions are explicit and narrow.** A request that fails a rule is admitted only through a recorded grant by an operator or arbiter (§12.6). The admitted event is exactly the request that was refused, signed by its original actor, and only one waivable code on an exceptable rule is waived. Authentication, authority, pins and review gating are never waived, so invariants 1–9 hold for every event, including excepted ones.

## 6. Identity and authority

### 6.1 Core classes

| Class | Purpose |
|---|---|
| `operator` | A human with final authority. |
| `coordinator` | Creates, assigns, closes and cancels tasks. |
| `worker` | Owns and performs tasks. |
| `reviewer` | Records reviews of results it was assigned to review. |
| `instrument` | Deterministic emitters (metrics, notifier). No task transitions in core. May advise on exceptions. |
| `arbiter` | Decides exception requests (§12.6), and nothing else. A hive registers an agent as an arbiter only after it has earned that authority (§12.6, promotion). |
| `gateway` | The gateway itself. Emits only `hive.initialized`, `gateway.started` and `gateway.rejected`. |

Extensions may add classes (§13). Each actor has exactly one class. A human who also wants to act as a worker registers a second actor.

### 6.2 Actors

Actor ids match `^[a-z0-9][a-z0-9._-]{0,63}$`. An actor exists from its `actor.registered` event and is `active` until `actor.retired`. A retired actor can't emit anything, and its id is never reused.

An actor's **declaration** records what the actor is: `harness`, `model_route`, `sandbox` (short strings), and optionally a `role_prompt` pin. It is set at registration and replaced by `actor.declared`. Agents MUST re-declare when their configuration changes, including self-modification.

### 6.3 Keys and signed requests

- **Identity is a public key.** Each actor has one or more public keys, recorded in the log (`actor.registered`, `actor.key_added`, `actor.key_revoked`). The fold of those events is the hive's address book: it says which actor a key belongs to. `GET /v1/actors` serves it.
- **Key types are pluggable.** A key is written `<type>:<base64url public key>` (base64url without padding, as are signatures). v1 requires `ed25519` (32-byte keys, 64-byte signatures). Other types (e.g. Nostr's `secp256k1` Schnorr) can be added in a later version without changing the envelope.
- **Every request is signed.** A write's body MUST be exactly the canonical request bytes (§7.2). The client sends:
  - `X-Hive-Key`: the public key;
  - `X-Hive-Signed-At`: RFC 3339 UTC time, `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`;
  - `X-Hive-Signature`: the signature over the UTF-8 bytes `hive1\n<hive id>\n<signed-at>\n<sha256 hex of the body>`.

  Because the signature covers the body bytes, the gateway can attribute any body, even one that isn't valid JSON, and record its refusal. A body that parses but isn't in canonical form is refused with `SCHEMA_INVALID`.

  Read requests have no body. They sign `hive1\n<hive id>\n<signed-at>\n<sha256 hex of "{}">\n<METHOD> <path?query>`, with the path and query exactly as sent.
- The gateway rejects a signature whose `signed-at` is more than 300 seconds from its clock. A replayed write is harmless: it has the same idempotency key and digest, so it returns the original event.
- A key is valid iff it has been added and not revoked, and its actor is active. Validity is a function of the fold, so the gateway keeps no separate key store.
- **Nothing secret enters the record.** Private keys never leave the actor's sandbox (§6.6).

### 6.4 Derivation

The gateway sets the envelope's `actor` = `{id, class}` of the key's owner, and stores `signature` = `{key, signed_at, sig}` on the event. The canonical request can be rebuilt from the stored event (§7.2), so anyone holding the log can re-verify every signature. A request body can't claim an actor: an `actor` field is an unknown top-level field, refused with `SCHEMA_INVALID` and recorded under the signer.

A request with a missing, invalid, expired or unknown signature gets HTTP 401 and is **not** recorded: it can't be attributed, and recording it would let anyone write to the log. The service logs it instead.

### 6.5 Channel attribution

An actor MAY send `X-Hive-Via: <label>` (max 64 chars, `^[a-z0-9][a-z0-9:._-]*$`), e.g. `telegram`, `mcp:chief-of-staff`. The gateway records it as `via`. It is attribution only and carries no authority.

### 6.6 Key custody (deployment obligation)

Signed requests only mean something if keys are held apart. A conforming deployment MUST ensure that:
- each agent actor runs in its own sandbox: no two agent actors share a process, container, filesystem, OS user or key;
- a private key is readable only inside its actor's sandbox;
- in particular, a worker's sandbox never holds a reviewer's key, so a worker can't record its own passing review.

The record can't check this by itself. It is part of the deployment acceptance test (§22.1).

## 7. Events

### 7.1 Envelope (`hive.event/1`)

| Field | Set by | Required | Semantics |
|---|---|---|---|
| `schema` | server | yes | `"hive.event/1"` |
| `hive` | server | yes | Hive id. |
| `position` | server | yes | Gapless integer ≥ 1. |
| `event_id` | server | yes | UUIDv7, lowercase canonical text. |
| `recorded_at` | server | yes | RFC 3339 UTC with microseconds. Informational; order is `position`. |
| `actor` | server | yes | `{id, class}` of the signing key's owner. |
| `signature` | server | yes, except gateway and applied events | `{key, signed_at, sig}`, as received (§6.4). On `gateway.rejected` it is the refused request's signature, which covers `data.refused_digest`. Events applied from a proposal (§24.4) carry none: their authority is the signed approval immediately before them. Events applied from an exception (§12.6) carry the original refused request's signature, which still verifies because the event is that exact request. |
| `via` | server | no | From `X-Hive-Via`. |
| `on_proposal` | server | no | Reserved for extensions (the `1-hive` profile's proposals, §24). |
| `on_exception` | server | no | The `exception_id` whose grant admitted this event (§12.6). |
| `verdict` | server | mirror mode only | `{legal: bool, code?, reason?}` (§12.5). |
| `type` | client | yes | Event type. |
| `task` | client | per type | Task id for task-scoped events. |
| `goal` | client | per profile | Reserved for profiles that define goals, which say where it is required; otherwise it MUST be absent. The server indexes task events by their task's goal (§18) but never adds the field to the envelope. |
| `basis` | client | yes | The highest position the client had observed when deciding. |
| `expected_revision` | client | no | For task events: the task revision the client acted on (§17.4). |
| `idempotency_key` | client | yes | 1–128 chars, `^[A-Za-z0-9._:-]+$`. |
| `refs` | client | yes (may be empty) | Array of `{rel, pin}` (§8). At most 16. |
| `data` | client | yes (may be `{}`) | Type-specific fields. May contain `ext` (§7.5). |

Ids for tasks, projects and extension entities are client-chosen, match `^[a-z0-9][a-z0-9._-]{0,63}$`, and are unique within the hive. Reuse is refused (`TASK_EXISTS`, and so on).

### 7.2 Request

A request is a JSON object with exactly `type`, `basis`, `idempotency_key`, `refs`, `data`, plus `task`, `goal` and `expected_revision` where used. Unknown top-level fields are refused (`SCHEMA_INVALID`). The generation travels in the `X-Hive-Generation` header, not in the body (§17.3).

The **canonical request** is the R0 canonical JSON of exactly these fields. Every one of them is copied into the event unchanged, which is what makes stored signatures verifiable.

### 7.3 Canonical encoding

An event's canonical bytes are the R0 canonical JSON of its envelope: UTF-8, no insignificant whitespace, keys in code-point order, no trailing LF. The gateway stores them together with `digest` = `sha256:<hex>`.

Numbers are integers with magnitude at most 2^53 − 1. Non-integer numbers are refused (`SCHEMA_INVALID`), so that every implementation produces identical bytes. Money is written in integer micro-units (e.g. `usd_micros`).

### 7.4 Size limits

- The request body is at most 64 KiB. A body over 1 MiB gets HTTP 413 and is not recorded; between the two limits it is authenticated and refused with a recorded `PAYLOAD_TOO_LARGE`.
- `data` canonical bytes are at most 8 KiB.
- Each string has the `maxLength` of its schema. If none is stated, it is 500.

A request over a limit is refused with `PAYLOAD_TOO_LARGE`.

### 7.5 Extension fields

`data.ext` is an optional object reserved for profile extensions. Core schemas allow `ext` only on the event types listed in §9 as "ext allowed"; the active profile's schemas define its contents. With the core profile, `ext` MUST be absent. Core rules and the core fold never read `ext`.

## 8. References and pin admission

Each `refs` entry is `{"rel": <rel>, "pin": <canonical R0 pin object>}`. Unknown fields are refused.

**Core rels:** `order`, `question`, `answer`, `result`, `review`, `report`, `policy`, `role_prompt`, `rationale`. Extensions may add rels.

For every ref, the gateway MUST:
1. check that the rel is required or allowed by the effective rule (`REF_MISSING`, `REF_NOT_ALLOWED`);
2. validate the pin against `pin-v1.schema.json` (`PIN_INVALID`);
3. run `hivepin.verify(pin)` with the publication check (not offline) against the hive's repository registry. On failure the refusal is `PIN_INVALID` with `detail.hivepin_code`; if the remote can't be reached it is `PIN_UNAVAILABLE`, which is `retryable: true`.

The registry is a file the gateway loads at start, and `registry_digest` is the sha256 of its canonical JSON. If the loaded registry's digest differs from the fold's `registry_digest`, every pin is refused with `PIN_UNAVAILABLE` (`detail.registry_mismatch: true`) until an operator records `hive.registry_changed` with the loaded digest or the gateway is restarted with the recorded registry.

The gateway MAY cache successful publication checks per `(repository, commit_oid)` for the life of its process. Pins MUST NOT be rewritten or normalized: the stored pin is exactly the one submitted.

## 9. Core event catalog

Fields are in `data`. "pin(rel)" means a ref with that rel. Each type has a JSON Schema in the policy tree (§10), and those schemas are authoritative for types and lengths.

| Type | Data | Refs | ext allowed |
|---|---|---|---|
| `hive.initialized` | `mode` ∈ {authoritative, mirror}; `profile` (name); `registry_digest`; `operator` {id, role, keys, declaration}, which registers the first operator | pin(policy) (a tree pin, §10) | no |
| `hive.policy_changed` | `profile`; `reason` | pin(policy) | no |
| `hive.registry_changed` | `registry_digest`; `reason` | — | no |
| `hive.mode_changed` | `mode` = `authoritative` | — | no |
| `project.opened` | `project`; `title` | — | no |
| `project.closed` | `project`; `reason` | — | no |
| `actor.registered` | `actor_id`; `class`; `role`; `keys` (1–8 public keys); `declaration` {harness, model_route, sandbox} | optional pin(role_prompt) | no |
| `actor.declared` | `actor_id`; `declaration` | optional pin(role_prompt) | no |
| `actor.retired` | `actor_id`; `reason` | — | no |
| `actor.key_added` | `actor_id`; `key` | — | no |
| `actor.key_revoked` | `actor_id`; `key`; `reason` | — | no |
| `task.created` | `title` (≤200); optional `project` | pin(order) | yes |
| `task.assigned` | `to` (actor id) | — | yes |
| `task.accepted` | — | — | yes |
| `task.declined` | `reason` | — | yes |
| `task.blocked` | `needs` ∈ {decision, information, access, external}; `reason` | pin(question) | yes |
| `task.answered` | optional `note` | pin(answer) | yes |
| `task.unblocked` | — | — | yes |
| `task.reported` | `kind` ∈ {progress, checkpoint, finding, reflection} | pin(report) | yes |
| `task.result_posted` | — | pin(result) | yes |
| `review.assigned` | `reviewer` (actor id); `result_event` (the current result) | — | yes |
| `review.recorded` | `verdict` ∈ {passed, failed, needs_information}; `result_event` (event_id) | pin(review) | yes |
| `task.closed` | optional `note` | — | yes |
| `task.reassigned` | `to`; `reason` | — | yes |
| `task.released` | `reason` | optional pin(report) | yes |
| `task.cancelled` | `reason` | — | yes |
| `exception.requested` | `exception_id`; `refusal` (the event_id of the actor's own `gateway.rejected`); `reason` (≤2000) | optional pin(rationale) | no |
| `exception.advised` | `exception_id`; `recommendation` ∈ {grant, deny}; `reason` (≤2000) | optional pin(rationale) | no |
| `exception.granted` | `exception_id`; `reason` (≤2000) | optional pin(rationale) | no |
| `exception.denied` | `exception_id`; `reason` (≤2000) | optional pin(rationale) | no |
| `exception.withdrawn` | `exception_id`; `reason` | — | no |
| `gateway.started` | `version`; `source_commit`; `policy_digest`; `registry_digest` (of the registry the gateway loaded) | — | no |
| `gateway.rejected` | `code`; `reason`; `retryable`; `by` {id, class}; `via`?; `refused` (the request if it parsed as a JSON object within the size limit, else null); `refused_digest`; `refused_size`; `detail` {} | — | no |

Type names outside the active profile's catalog are refused (`UNKNOWN_EVENT_TYPE`). The prefixes `message.`, `skill.`, `route.`, `trial.` and `memory.` are reserved for R4–R10 and must not be used by extensions.

## 10. Policy trees

A hive's policy is a git **tree** pinned with R0. The tree has this layout:

```
policy/
  core/
    legality.json                  # the core legality table (§11)
    schemas/events/<type>.schema.json
  profiles/<name>/
    profile.json                   # the extension (§13)
    schemas/events/<type>.schema.json    # schemas for new types
    schemas/ext/<type>.schema.json       # schemas for data.ext on core types
```

`hive.initialized` and `hive.policy_changed` pin the tree and name the `profile`: either `core` or a directory under `profiles/`. The gateway materializes the tree through `hivepin`, composes the profile (§13.2) and validates it. It refuses to start if any of that fails.

The canonical v1 tree is published in this repository under `policy/`, and v1 ships two profiles: `core` and `1-hive`.

## 11. The core legality table

### 11.1 Rule format

The table is `policy/core/legality.json`, validated by `schemas/legality-table-v1.schema.json`. It declares the core `classes`, `rels` and `codes`, the entity kinds, the core inbox (§16.2) and the rules:

```jsonc
{
  "schema": "hive.legality/1",
  "version": "1.0.0",
  "classes": ["operator", "coordinator", "worker", "reviewer", "instrument", "arbiter", "gateway"],
  "rels": ["order", "question", ...],
  "codes": ["SCHEMA_INVALID", ...],
  "entities": {
    "task": {"collection": "tasks", "id": "task", "state_field": "status",
             "states": ["created", ...], "terminal": ["done", "cancelled"],
             "unknown_code": "UNKNOWN_TASK", "exists_code": "TASK_EXISTS",
             "illegal_code": "ILLEGAL_TRANSITION", "mirrorable": true}
  },
  "inbox": {"operator": ["task_blocked_unanswered", ...]},
  "rules": [
    {
      "event": "review.recorded",
      "entity": "task",                       // hive | project | actor | task | (extension entities)
      "classes": ["reviewer", "operator"],
      "relation": null,                       // null | "owner" | "self" | "proposer"
      "from": ["in_review"],                  // null | "*" | "open" | a list of states
      "to": {"by": "verdict", "map": {"passed": "in_review", "failed": "in_progress", "needs_information": "in_progress"}},
      "refs": {"required": ["review"], "allowed": []},
      "conditions": ["targets_current_result", "reviewer_not_author", "reviewer_is_assigned"],
      "effects": ["record_review"],
      "exceptable": false                     // may an exception waive this rule? (§12.6)
    }
  ]
}
```

- **Entities.** Each entity kind names where its id comes from (`task`, `goal`, or a `data.<field>`; the hive is a singleton), the field holding its state, its states and terminal states, the codes for "unknown", "already exists" and "not allowed from this state" (steps 4 and 5 of §12.1), and whether it is `mirrorable` (§12.5). The core kinds are `hive` (its state is the mode), `project` (`PROJECT_NOT_OPEN` from a wrong state), `actor` (`ACTOR_RETIRED`) and `task`.
- **`classes`** is a list, or `"*"` for every class except `gateway`.
- **`from`** is `null` (the entity must not exist yet), `"*"` (any state), `"open"` (any non-terminal state) or a list of states.
- **`to`** is a state name, `"same"`, or `{by, map}`, where the new state is chosen by a `data` field.
- **`relation`** narrows who, within the allowed classes, may emit:
  - `owner`: an actor of class `worker` or `reviewer` must be the task's current owner. Other classes are unaffected.
  - `self`: a non-operator actor must be the subject (`data.actor_id`).
  - `proposer`: a non-operator actor must be the proposal's proposer.
  - `requester`: a non-operator actor must be the exception's requester.
  A relation failure is `NOT_OWNER`.
- The **target** of an event is `data.to`, or `data.reviewer` for `review.assigned`.
- **Conditions and effects** are names from the fixed vocabularies in §15, optionally with arguments: `{"name": "target_active_class", "args": ["worker", "reviewer"]}`.
- **`exceptable`** (default `false`) says whether an exception may admit a refused request of this type (§12.6). Only rules on task and extension entities may set it.
- There is exactly one rule per event type.

### 11.2 Task lifecycle (core)

States: `created`, `assigned`, `in_progress`, `blocked`, `in_review`, `done`, `cancelled`. `done` and `cancelled` are terminal. "Open" means non-terminal.

```
  created ──assigned──▶ assigned ──accepted──▶ in_progress ──result_posted──▶ in_review ──closed*──▶ done
     ▲                     │                    │     ▲                          │
     │                     │                blocked  unblocked         review(failed)
     │                     │                    ▼     │                          │
     │                     │                    blocked                          ▼
     │                     │                                                in_progress
     └── declined ─────────┘
     └── released ◀── assigned | in_progress | blocked      (the owner gives up)
         reassigned: assigned | in_progress | blocked ──▶ assigned (new owner)
         cancelled:  any open ──▶ cancelled

  * closed requires: latest review of the current result = passed, by the assigned reviewer (or an operator), never the author
```

The effects of each rule are listed in `policy/core/legality.json`, which is authoritative. Every task rule is `exceptable` except `review.assigned`, `review.recorded` and `task.closed`: the review gate is never waived.

| Event | Classes; relation | From → To | Conditions |
|---|---|---|---|
| `task.created` | coordinator, operator | ∅ → created | `project_open_if_given` |
| `task.assigned` | coordinator, operator | created → assigned | `target_active_class(worker, reviewer)` |
| `task.accepted` | worker, reviewer; owner | assigned → in_progress | — |
| `task.declined` | worker, reviewer; owner | assigned → created | — |
| `task.blocked` | worker, reviewer; owner | in_progress → blocked | — |
| `task.answered` | coordinator, operator | blocked → blocked | — |
| `task.unblocked` | worker, reviewer; owner | blocked → in_progress | `answered_if_needed` |
| `task.reported` | worker, reviewer; owner | assigned, in_progress, blocked, in_review → same | — |
| `task.result_posted` | worker, reviewer; owner | in_progress → in_review | — |
| `review.assigned` | coordinator, operator | in_review → same | `targets_current_result`; `target_active_class(reviewer)`; `assigned_reviewer_not_author` |
| `review.recorded` | reviewer, operator | in_review → in_review (passed) · in_progress (failed, needs_information) | `targets_current_result`; `reviewer_not_author`; `reviewer_is_assigned` |
| `task.closed` | coordinator, operator | in_review → done | `current_result_passed` |
| `task.reassigned` | coordinator, operator | assigned, in_progress, blocked → assigned | `target_active_class(worker, reviewer)`; `target_is_not_owner` |
| `task.released` | worker, reviewer; owner | assigned, in_progress, blocked → created | — |
| `task.cancelled` | operator | open → cancelled | — |

### 11.3 Hive, project and actor rules (core)

| Event | Classes; relation | From → To | Conditions |
|---|---|---|---|
| `hive.initialized` | gateway | ∅ → `data.mode` | `log_empty` |
| `hive.policy_changed`, `hive.registry_changed` | operator | any → same | — |
| `hive.mode_changed` | operator | mirror → authoritative | — |
| `gateway.started`, `gateway.rejected` | gateway | any → same | — |
| `project.opened` | coordinator, operator | ∅ → open | — |
| `project.closed` | operator | open → closed | `project_has_no_open_work` |
| `actor.registered` | operator | ∅ → active | `class_registrable`; `keys_unused` |
| `actor.declared` | any class except gateway; self | active → same | — |
| `actor.retired` | operator | active → retired | `not_last_operator` |
| `actor.key_added` | operator | active → same | `key_unused` |
| `actor.key_revoked` | any class except gateway; self | any → same | `key_is_actors` |
| `exception.requested` | any class except gateway | ∅ → pending | `refusal_is_own`; `refusal_waivable`; `refusal_not_excepted` |
| `exception.advised` | arbiter, coordinator, instrument | pending → same | `not_requester` |
| `exception.granted` | operator, arbiter | pending → granted | `not_requester`; `exception_applicable` (application, §12.6) |
| `exception.denied` | operator, arbiter | pending → denied | `not_requester` |
| `exception.withdrawn` | any class except gateway; requester | pending → withdrawn | — |

The actor id `gateway` is reserved for the gateway's own events. `gateway.rejected` is never evaluated by the gate; its rule exists so that every event type has one.

## 12. The gate

### 12.1 Evaluation order

For a request `r` from actor `a`, against the effective profile and state `S` = the fold at the current head, the checks run in this order, and the first failure decides the code:

1. Authentication (§6.4), then envelope and data schema validity, including `ext` against the profile (`SCHEMA_INVALID`, `UNSUPPORTED_SCHEMA`, `PAYLOAD_TOO_LARGE`, `UNKNOWN_EVENT_TYPE`).
2. Generation and basis (§17.2–17.3), then idempotency (§17.1), then expected revision (§17.4, `STALE_REVISION`).
3. Rule lookup. Class check (`NOT_AUTHORIZED`), then relation check (`NOT_OWNER`).
4. Entity existence (`UNKNOWN_*`, or the `*_EXISTS` codes).
5. From-state (`ILLEGAL_TRANSITION`).
6. Refs rels (`REF_MISSING`, `REF_NOT_ALLOWED`), then conditions in listed order.
7. Pin verification (§8), last, because it touches the network.

If every check passes, the event is appended and the fold applies the rule's `to` state and effects. Evaluation and append MUST happen under the gateway's write lock against the current head.

### 12.2 Refusals

A refused request from an authenticated actor produces exactly one `gateway.rejected` event (actor = gateway) and nothing else. There is no coalescing: repeated refusals are repeated events.

### 12.3 Core refusal codes

`SCHEMA_INVALID`, `UNSUPPORTED_SCHEMA` (reserved for a future request schema; unreachable in v1), `UNKNOWN_EVENT_TYPE`, `PAYLOAD_TOO_LARGE`, `STALE_GENERATION`, `STALE_REVISION`, `IDEMPOTENCY_CONFLICT`, `KEY_EXISTS`, `UNKNOWN_KEY`, `NOT_AUTHORIZED`, `NOT_OWNER`, `UNKNOWN_ACTOR`, `ACTOR_RETIRED`, `ACTOR_EXISTS`, `UNKNOWN_PROJECT`, `PROJECT_EXISTS`, `PROJECT_NOT_OPEN`, `PROJECT_HAS_OPEN_WORK`, `UNKNOWN_TASK`, `TASK_EXISTS`, `ILLEGAL_TRANSITION`, `INVALID_ASSIGNEE`, `NOT_ANSWERED`, `REF_MISSING`, `REF_NOT_ALLOWED`, `PIN_INVALID`, `PIN_UNAVAILABLE`, `POLICY_INVALID` (a policy pin whose tree or profile doesn't load, checked at step 7), `REVIEW_REQUIRED`, `REVIEW_STALE`, `REVIEW_NOT_INDEPENDENT`, `REVIEW_NOT_ASSIGNED`, `LAST_OPERATOR`, `UNKNOWN_EXCEPTION`, `EXCEPTION_EXISTS`, `EXCEPTION_NOT_PENDING`, `REFUSAL_MISMATCH`, `NOT_EXCEPTABLE`, `EXCEPTION_NOT_APPLICABLE`, `INTERNAL_ERROR`.

Only `PIN_UNAVAILABLE` and `INTERNAL_ERROR` are `retryable: true`. Extensions may add codes. Clients MUST rely on codes, not on reason text.

### 12.4 Policy versions

The fold MUST evaluate each event with the profile in force at that event's position. That is the one pinned by `hive.initialized`, or by the latest earlier `hive.policy_changed`.

### 12.5 Modes

- **authoritative:** a failed check refuses the request.
- **mirror:** a request on a task (or an extension entity with a `violations` count) that fails only at step 3, 5 or 6 is **appended** with `verdict: {legal: false, code, reason}`. Its `to` state and effects are applied anyway, and the entity's `violations` count goes up by one. Everything else is still refused: authentication, schema, idempotency, revision, entity existence (step 4, since there is nothing to apply to, or applying would overwrite) and pin failures, and every failure on hive, project, actor or proposal events, since identity and authority are never recorded as illegal-but-applied.

The mode is set by `hive.initialized`, and the only allowed change is mirror → authoritative. Mirror mode is the release plan's shadow adoption: a hive records its existing flow first, then flips. How a mirror adapter maps a source hive's actors is out of scope for v1.

### 12.6 Exceptions

Rules decide who may make which coordination move. Judgment about the work itself (is it good, relevant, stuck, worth doing) is made by actors, including LLM agents, and enters the record as their signed events, which the rules require. Exceptions cover the remaining case: a legitimate move that the rules forbid, e.g. reopening a task that was closed too early.

**What can be waived.** One refusal code, and only if:
- the refused request's rule is `exceptable` (§11.1);
- for a task event, the refused request carried `expected_revision`, so a grant can only apply to the exact situation the arbiter looked at; and
- the code is **waivable**: `ILLEGAL_TRANSITION` (step 5), `INVALID_ASSIGNEE`, `NOT_ANSWERED` or `PROJECT_NOT_OPEN`, plus codes an extension declares waivable for its own conditions (§13.1). The core table lists these four as `waivable_codes`.

Never waivable: authentication, schema and size (step 1); generation, idempotency and revision (step 2); class and relation, i.e. authority (step 3); existence (step 4); refs and pins (steps 6–7); and the review codes. An exception can only admit an event type the rules define; it can't invent new moves.

**Flow.**
1. The request is refused as usual, and the refusal is recorded.
2. The requester sends `exception.requested`, naming its own refusal and giving its reasons.
3. Optionally, actors send `exception.advised` (grant or deny, with reasons). This is how an agent is tried out as an arbiter in shadow.
4. An operator or arbiter, never the requester, sends `exception.granted` or `exception.denied`.

**Application.** On `exception.granted`, the refused request (stored in the refusal, §9) is evaluated as if its requester had sent it again against the current head: expected revision (§17.4), then steps 3–7, with the refused code waived wherever it arises in steps 5–6. The requester must still be active (otherwise the inner code is `ACTOR_RETIRED`), the refused request's signing key must still be one of its active keys (`UNKNOWN_KEY`), its idempotency key must be unused (`IDEMPOTENCY_CONFLICT`), and, under the profile now in force, its rule must still be exceptable and its code waivable (`NOT_EXCEPTABLE`).
- If it is admissible, the gateway appends **two consecutive events atomically**: `exception.granted`, then the applied event. The applied event is the refused request exactly (`type`, `task`, `goal`, `basis`, `expected_revision`, `idempotency_key`, `refs`, `data`), with `actor` = the requester, `signature` = the refused request's signature, `via` = the refusal's via, and `on_exception` = the exception id. Because the body is unchanged, the requester's signature still verifies. The idempotency key is now consumed, so a retry of the original request returns this event.
- If it fails again, for any reason including a different code, the grant is refused with `EXCEPTION_NOT_APPLICABLE` and `detail.inner_code`, and the exception stays `pending`. In particular, if the task has changed since the refusal, the revision check fails: the arbiter decided on a situation that no longer exists.

**Promotion of an arbiter (guidance).** A hive can try an agent as an arbiter without giving it authority: register it as an `instrument`, let it send `exception.advised` on real requests, and compare its advice with the operator's decisions against a criterion written down in advance. If it meets the criterion, the operator registers it as an `arbiter`. The record holds the evidence for the decision.

**Rules that keep needing exceptions should change.** Every exception is on the record, with its code and rule. A pattern of grants for the same rule is the signal to change the policy (`hive.policy_changed`), not to keep relying on arbiters.

## 13. Profiles and extensions

### 13.1 What an extension may contain

`profile.json` (schema `hive.profile/1`) contains:

- `name`, `version`, `extends: "core@1"`.
- `add_classes`: new class names.
- `add_rels`: new ref rels.
- `add_codes`: new refusal codes.
- `entities`: new entity kinds, with their state lists.
- `rules`: full rules (§11.1) for **new** event types only.
- `amend`: for **core** event types, a list of amendments. Each amendment may:
  - `add_classes`, limited to classes added by this extension;
  - `add_conditions`;
  - `add_effects`, limited to effects that write only extension state (§15.2);
  - `add_refs` (required or allowed);
  - `require_ext`: fields of `data.ext` that become mandatory. A missing required field is refused with `SCHEMA_INVALID`;
  - `require_goal`: the envelope `goal` becomes mandatory;
  - `forbid_exceptions`: the core rule becomes non-exceptable.
- `waivable_codes`: codes of this extension's own conditions that exceptions may waive. Extension rules may set `exceptable`.
- `inbox`: the inbox for each class under this profile, as names of inbox rules (§16.2). It replaces the core inbox.
- `hooks`: extension effects the fold runs `on_touch` (whenever `touch_activity` applies) and `on_terminal` (whenever an entity reaches a terminal state).

`profile.json` is validated by `schemas/profile-v1.schema.json`.

### 13.2 Composition

The effective policy is the core table with every amendment applied, plus the extension's rules. Composition is deterministic, and the result is what the gate and the fold execute.

### 13.3 Add or tighten, never loosen

A valid extension MUST NOT:
- change any core rule's `from`, `to` or `relation`;
- remove, or grant to an existing core class, any permission of a core rule;
- remove any condition or required ref of a core rule;
- define rules for core event types (amendments only);
- define a new event type that creates or moves a core entity (its rule must have `to: "same"` and a non-null `from`), or that uses a core effect;
- use a core effect in an amendment or a hook;
- redefine a core class, entity or schema;
- make a core rule exceptable, or make a core condition's code waivable;
- use a reserved type prefix.

The gateway refuses to load an extension that breaks any of these. A consequence: every core invariant (§5) holds under every profile. The acceptance suite (§22) runs the core property tests against every shipped profile.

**Promotion:** when a hive has evidence that an extension mechanism works, it can be proposed for the core in a new core version. The rest of One Hive then adopts it, instead of every extension being forced on everyone.

## 14. Fold state

### 14.1 Core state (normative)

The state is one object: `{"hive": …, "actors": {id: …}, "projects": {id: …}, "tasks": {id: …}, "exceptions": {id: …}}`, plus one collection per extension entity kind, which appears when its first entity is created. `hive` is `null` before `hive.initialized`.

- **hive:** `hive`, `mode`, `profile`, `policy` (pin), `registry_digest`, `head`, `gateway` {version, source_commit, registry_digest, since, position} (from the latest `gateway.started`).
- **actor:** `id`, `class`, `role`, `declaration`, `role_prompt`, `status` ∈ {active, retired}, `keys` (active public keys, sorted), `revoked_keys` (sorted), `registered_at`, `status_since`.
- **project:** `id`, `title`, `status` ∈ {open, closed}, `opened_at`, `status_since`.
- **task:** `id`, `project`, `goal` (from the creating request, or null), `title`, `order` (pin), `status`, `status_since`, `revision`, `owner`, `assigned_at`, `last_activity` {position, at}, `block` {needs, reason, question, answered, answer, position} or null, `current_result` {event_id, pin, author, at, position} or null, `assigned_reviewer` for the current result or null, `latest_review` {event_id, verdict, reviewer, review_pin, position} for the current result or null, `results_count`, `reviews_failed_count`, `reassignments`, `exceptions_applied`, `violations`, `created_by`, `created_at`, `ext` {}.
- **exception:** `id`, `requester`, `refusal` {event_id, position, code, type, task}, `reason`, `status` ∈ {pending, granted, denied, withdrawn}, `status_since`, `advice` [{actor, recommendation, position}], `decided_by`, `decided_at`, `applied_event` {event_id, position} or null.

`status_since` is the position of the event that last changed the entity's state. The `position` fields let inbox items name their causing event (§16.2).

### 14.2 Extension state

Extensions keep their state under `ext` on core entities, and in their own top-level collections (e.g. `goals`, `proposals`). Core effects never read extension state.

### 14.3 Determinism

Two conforming folds of the same log MUST produce byte-identical canonical JSON state. Sets serialize as sorted arrays. Maps serialize in code-point key order.

## 15. Vocabularies

The engine implements one fixed vocabulary shared by all profiles. Tables and extensions select from it. A new vocabulary entry requires a new engine version.

### 15.1 Conditions

| Condition | Holds when | Failure code |
|---|---|---|
| `log_empty` | head = 0 | `ILLEGAL_TRANSITION` |
| `class_registrable` | `data.class` exists in the profile and ≠ `gateway` | `NOT_AUTHORIZED` |
| `keys_unused` | none of `data.keys` is registered to any actor, active or retired | `KEY_EXISTS` |
| `not_last_operator` | the retired actor isn't the last active operator | `LAST_OPERATOR` |
| `project_open_if_given` | `data.project` is absent, or it exists and is open | `UNKNOWN_PROJECT`, `PROJECT_NOT_OPEN` |
| `project_has_no_open_work` | no open task (and, per extension, no open goal) in the project | `PROJECT_HAS_OPEN_WORK` |
| `target_active_class(c…)` | the target (§11.1) is an active actor of one of classes `c` | `INVALID_ASSIGNEE` |
| `target_is_not_owner` | the target ≠ the current owner | `INVALID_ASSIGNEE` |
| `answered_if_needed` | the block's `needs` ∈ {access, external}, or a `task.answered` exists since the block | `NOT_ANSWERED` |
| `targets_current_result` | `data.result_event` = the current result | `REVIEW_STALE` |
| `reviewer_not_author` | the actor ≠ the author of the current result | `REVIEW_NOT_INDEPENDENT` |
| `assigned_reviewer_not_author` | `data.reviewer` ≠ the author of the current result | `REVIEW_NOT_INDEPENDENT` |
| `reviewer_is_assigned` | the actor is an operator, or is the reviewer assigned to the current result | `REVIEW_NOT_ASSIGNED` |
| `key_unused` | `data.key` isn't registered to any actor, active or retired | `KEY_EXISTS` |
| `key_is_actors` | `data.key` is an active key of `data.actor_id` | `UNKNOWN_KEY` |
| `current_result_passed` | the latest review of the current result passed | `REVIEW_REQUIRED` |
| `refusal_is_own` | `data.refusal` is a `gateway.rejected` event whose `by.id` is the actor and whose `refused` is not null | `REFUSAL_MISMATCH` |
| `refusal_waivable` | the refused request's rule is `exceptable`, its code is waivable (§12.6), and, for a task event, it carried `expected_revision` | `NOT_EXCEPTABLE` |
| `refusal_not_excepted` | no other exception for the same refusal is `pending` or `granted` | `EXCEPTION_EXISTS` |
| `not_requester` | the actor isn't the exception's requester | `NOT_AUTHORIZED` |
| `exception_applicable` | the refused request is admissible with its code waived (§12.6, Application) | `EXCEPTION_NOT_APPLICABLE` |
| *extension conditions* | defined in §25 | per §25 |

### 15.2 Effects

The fold applies a rule in this order: create the entity (when `from` is null) with `id`, its state and `status_since`, plus `violations: 0` if it is mirrorable; set the `to` state; run the rule's effects in order; count a violation (§12.5); then, for tasks, increment `revision` and apply `touch_activity`; finally run the `on_terminal` hooks if the entity just reached a terminal state.

**Core effects** write core state only:
- `init_hive` (also registers the first operator), `set_policy`, `set_registry`, `record_gateway`;
- `open_project`;
- `register_actor`, `update_declaration`, `add_key`, `revoke_key`;
- `create_task`, `set_owner`, `clear_owner`, `count_reassignment`;
- `record_block`, `record_answer`, `clear_block`;
- `set_current_result`, which also clears the latest review and the assigned reviewer;
- `assign_reviewer`, `record_review`;
- `create_exception` (copies the refusal's code, type and task), `record_advice`, `decide_exception`. On a grant, the applied event's fold also increments the task's `exceptions_applied` and sets the exception's `applied_event`.

`touch_activity` is implied for every event on a task by the actor that owned the task before the event: it sets `last_activity` and runs the profile's `on_touch` hooks. State changes such as retiring an actor or closing a project are the rule's `to` state, not effects.

**Extension effects** write only extension state. They are listed in §25.

## 16. Projections

### 16.1 Board

`hive board [--project] [--at]` renders open tasks by state with owner, last activity and review status (plus extension marks, when the profile defines them). It is a pure rendering of the fold.

### 16.2 Inbox

The inbox is a pure function of the fold and the source of all human notifications. Tables and profiles list, per class, names from a fixed vocabulary of inbox rules. **Core inbox, for `operator`:**
- `task_blocked_unanswered`: tasks `blocked` with `needs` ∈ {decision, information} and not answered;
- `task_passed_not_closed`: tasks `in_review` whose current result has a passed review;
- `task_review_unassigned`: tasks `in_review` with no reviewer assigned to the current result;
- `exception_pending`: exceptions in `pending`.

**Core inbox, for `arbiter`:** `exception_pending`.

Profiles replace the inbox (§26). An item is `{kind, entity, id, position, title, key}`, where `position` is the causing event and `key` = `<kind>:<entity id>:<position>` is stable, so notifiers can de-duplicate. Items are ordered by position.

### 16.3 Export and standalone fold

`hive export [--after]` writes canonical JSONL. `hive fold <file> --policy <dir>` computes the state without a database.

## 17. Idempotency, basis, generation

### 17.1 Idempotency

`(actor, idempotency_key)` identifies an accepted event.
- A repeat whose request digest (sha256 of the canonical request) is equal returns the original event with `duplicate: true`.
- A repeat with a different digest is refused with `IDEMPOTENCY_CONFLICT`.
- Refused requests don't consume keys.

### 17.2 Basis

`basis` MUST satisfy 0 ≤ basis ≤ head; otherwise the request is refused with `STALE_GENERATION`. It records what the actor had seen when it decided, for replay. It isn't otherwise gated.

### 17.3 Generation

- The log has a **generation**: an opaque id created at initialization and replaced by the restore procedure. It is stored outside the log, writable only by the admin role.
- Every response carries it.
- A request carrying a stale `X-Hive-Generation`, or a read cursor from an old generation, is refused with `STALE_GENERATION` (recorded for writes; HTTP 409 for reads), and the client MUST drop its cursor and re-read.

### 17.4 Task revision

- Every task has a `revision`: 1 at creation, plus 1 for every accepted event on that task.
- A task request MAY carry `expected_revision`. If it differs from the task's revision, the request is refused with `STALE_REVISION`.
- This catches decisions made on stale information even when the transition itself would still be legal, e.g. a coordinator closing a task based on an old reading. Clients acting on a task they just read SHOULD send it. The CLI and the operating loop always do.

## 18. Storage guarantees (Postgres)

- **Roles:**
  - `hive_admin` owns the schema; it is used only by migrations and restore.
  - `hive_gateway`: `INSERT` on `events`, and `SELECT`.
  - `hive_reader`: `SELECT` only.
- No role other than `hive_admin` has `UPDATE`, `DELETE` or `TRUNCATE`. A trigger on `events` MUST raise on `UPDATE` and `DELETE` for every role.
- New tables are readable by `hive_reader` by default and never writable by default.
- **`events` table:** `position BIGINT PRIMARY KEY`, `event_id UUID UNIQUE`, `recorded_at`, `type`, `actor_id`, `goal_id`, `task_id`, `idempotency_key`, `request_digest`, `canonical TEXT`, `digest TEXT`, `envelope JSONB` (which includes the signature). There is a unique index on `(actor_id, idempotency_key)` that excludes `gateway.rejected`.
- **Gapless positions:** head + 1, allocated under a transaction-scoped advisory lock held for the whole gate-and-append.
- **`hive_meta` table:** the hive id and the generation (§17.3). Only `hive_admin` writes it.
- **Restore:** as `hive_admin`, replace the tables with a canonical JSONL export (`hive restore`) and set a new generation. Events after the export are lost. Operators SHOULD run the log with WAL archiving if that matters.
- **Gateway process.** The gateway keeps the fold in memory. Before each gate-and-append it takes the advisory lock and compares the stored generation and head with its own; if either differs (a restore, or another gateway process), it reloads from the table first.
- **Gateway events.** Events whose actor is the gateway use the reserved actor `{"id": "gateway", "class": "gateway"}`, `basis` = the head before them and `idempotency_key` = `gateway:<position>`.

## 19. HTTP API (v1)

JSON bodies (UTF-8). Every response includes `generation` and `head`. Every endpoint except health requires a signed request (§6.3). In v1, every active actor may read everything.

### 19.1 Write

`POST /v1/events`, with a request body (§7.2).
- `201 {"status":"accepted","event":…}` on admission.
- `200 {"status":"duplicate","event":…}` for an idempotent repeat.
- `422 {"status":"refused","code","reason","retryable","refusal":<event>}` for a recorded refusal.
- `401` unauthenticated (not recorded); `413` over the request limit (recorded between 64 KiB and 1 MiB, §7.4); `409` stale generation or basis beyond head (recorded).
- `400` for a malformed `X-Hive-Via` (not recorded).

The gateway re-checks the signing key against the fold under its write lock, so a key revoked a moment earlier can't be used.

If an extension defines events that append more than one event atomically (e.g. proposal approval, §24), the response includes all of them in `events`.

### 19.2 Read

- `GET /v1/events?after=&limit=(≤1000)&type=&task=&goal=` → `{events}`. `goal` matches events on the goal and on its tasks.
- `GET /v1/subscribe?after=`: Server-Sent Events. Each event is `event: event`, `id:` = position, `data:` = the canonical envelope. It is resumable with `Last-Event-ID`. After a restore the stream sends `event: stale_generation` and ends.
- `GET /v1/state?at=` → `{at, state}`: the fold at a position (default: head).
- `GET /v1/tasks/{id}`: the task's state plus its event history. Extensions add entity endpoints, e.g. `/v1/goals/{id}`.
- `GET /v1/inbox?for=<class>`.
- `GET /v1/actors`: the address book (actors, classes, active keys, declarations).

A read carrying an `X-Hive-Generation` that isn't current gets `409 STALE_GENERATION` (not recorded).

### 19.3 Health

`GET /v1/health` (no authentication) → `{status, hive, head, generation, profile, policy_version, gateway_version}`.

On every start, the gateway appends `gateway.started` with its version and source commit before admitting any request. This makes it possible to tell which implementation made each decision.

### 19.4 Keys

Keys are public, so there is no special endpoint: an operator adds or revokes keys with ordinary `actor.key_added` and `actor.key_revoked` events. `hive keygen` creates an ed25519 key pair inside the calling sandbox and prints only the public key.

### 19.5 Bootstrap

`hive init --hive <id> --profile <name> --policy <pin> --mode <mode>` runs as `hive_admin`. It takes the first operator's **public** key (`--operator-key`). It creates the schema and generation, and appends `hive.initialized` (actor = gateway), whose effect also registers the first operator with that key. No secret is created or printed. It refuses a non-empty log.

## 20. Adoption paths

1. **Audit first (R1):** `core` profile in `mirror` mode. A mirror adapter or the hive's own agents write through the API, and nothing is refused. The hive gets an audit trail, history and a board.
2. **Enforce (R2):** flip to `authoritative`. Illegal transitions and unreviewed closes are refused.
3. **Opt into extensions:** re-pin the policy with an extension profile, e.g. `1-hive`, via `hive.policy_changed`.

Each step is a recorded event, and no step requires the operating loop of any other hive.

## 21. Conformance fixtures

`fixtures/<profile>/<name>/` contains:
- `log.jsonl`: the canonical log;
- `policy`: the pin of the policy tree the log uses (the fixture repository's `policy/`);
- `expected/state@<pos>.json` and `expected/inbox@<pos>.json` (every inbox class of the profile in force);
- `requests.jsonl` plus `expected/responses.jsonl`, replayed against a scratch gateway.

Pins reference a fixture repository created by the deterministic `fixtures/make-repo.sh` at `/tmp/hive-record-fixture` (its registry holds absolute paths, so golden files assume that location). `fixtures/build.py` regenerates every fixture and asserts the intended outcome of each step.

**Replay.** The gateway's clock is set to each step's `at`, and event ids come from a UUIDv7 generator seeded with 0 (Python's `random.Random(0)`: 12 bits, then 62 bits, per id). Under these conditions a conforming gateway reproduces `log.jsonl` byte for byte. Each line of `requests.jsonl` is one step:
- `{"op": "init", "at", "hive", "mode", "profile", "policy", "registry_digest", "operator"}`: `hive init`;
- `{"op": "start", "at", "data"}`: the gateway starts and appends `gateway.started` with `data`;
- `{"op": "request", "at", "actor", "request" | "body", …}`: a write signed with the actor's fixture key, derived as `sha256("hive-fixture-key:" + id)` for the ed25519 seed. Optional fields: `signer` (sign with another id's key), `signed_at` (default `at`), `sign_hive` (sign for another hive id), `generation: "stale"` (send a wrong `X-Hive-Generation`), `pad` (add `data.pad` = that many `x` characters), and `body` (send these exact bytes instead of the canonical request).

A response line is `{"status", "http", "positions"?, "code"?}`, where `status` is `accepted`, `duplicate`, `refused` or `unauthenticated`.

**Core fixtures:** `happy-path`, `review-loop`, `stale-review`, `block-answer`, `reassign`, `policy-change`, `mirror-mode`, `mode-flip`, `exception` (a task closed too early is reopened with a granted exception on `task.reassigned`, then reviewed and closed again; plus a denied request and a stale grant), and `refusals` (one per reachable core code).

## 22. Acceptance tests

### 22.1 R1

- Fixtures pass through the real gateway and through the standalone fold with identical state.
- `hive_reader` can't insert; `hive_gateway` can't update or delete; the trigger blocks the owner too.
- Positions are gapless under concurrent writers.
- Idempotent retries under concurrency produce exactly one event.
- A body claiming another actor is recorded under the signing key's actor.
- Every stored signature re-verifies from the exported log alone.
- Expired, unknown, revoked and malformed signatures get 401 and leave the log unchanged.
- **Deployment:** each agent actor runs in its own sandbox, and no sandbox can read another actor's private key (§6.6). A worker process that tries to sign with a reviewer key finds no key to use.
- An unauthenticated request leaves the log unchanged.
- A restore changes the generation, and old cursors get `STALE_GENERATION`.
- Pins: unpublished, tampered, unknown-repository and remote-down cases produce the specified codes.
- SSE delivers each event exactly once and resumes correctly.
- A scratch hive (`hive scratch up/down`) leaves no processes, containers or databases behind.
- An extension that breaks §13.3 is refused at load.

### 22.2 R2: deliberate-failure suite (core)

For each case, the test asserts: refused with the specified code; a `gateway.rejected` event recorded (when authenticated); state unchanged.

- **Class:** a worker creates, assigns, closes or cancels; an instrument makes a task transition; the gateway class can't be registered.
- **Owner:** accept, block, report or post a result on another actor's task.
- **State:** accept unassigned; post a result from `blocked`; close from `in_progress`; any transition out of `done` or `cancelled`.
- **Review:** close with no review; close after a failed or `needs_information` review; close after a pass on a superseded result; a review by the author; a review from the worker class; a review by a reviewer not assigned to the current result; a reviewer assigned by the task's owner; assigning the author as reviewer.
- **Revision:** any task request with a stale `expected_revision`.
- **Refs:** a missing required rel; a disallowed rel; a malformed, tampered or unpublished pin.
- **Identity:** a retired actor; a revoked key; adding a key already registered; retiring the last operator.
- **Structure:** an unknown type; a reserved prefix; an unknown field; `ext` under the core profile; oversized data; an idempotency conflict; basis > head.
- **Exceptions:** requesting an exception for another actor's refusal; for a non-waivable code (e.g. `REVIEW_REQUIRED`, `NOT_AUTHORIZED`, `PIN_INVALID`); for a non-exceptable rule (`task.closed`); twice for the same refusal; granting or denying by the requester, a worker or the chief of staff; granting twice; granting after the task changed (`EXCEPTION_NOT_APPLICABLE` with inner `STALE_REVISION`); withdrawing someone else's request.

### 22.3 Properties, run under every shipped profile

Random sequences of well-formed requests from random actors, in both modes, check:
- `done` ⇒ the latest review of the current result passed, its reviewer ≠ the author, and the reviewer was assigned to that result or is an operator (in mirror mode: for every task with `violations` = 0, since a close can be legal against state that an earlier illegal event produced);
- in authoritative mode, every appended event passed its guard, except events with `on_exception`, each of which failed only its exception's waived code, immediately follows its `exception.granted`, and carries a signature that verifies against its own canonical request;
- the fold is deterministic, and the gateway's state = the standalone fold of the export;
- positions are gapless, and each digest matches its canonical bytes.

## 23. Deliverables and definition of done

**Deliverables:**
1. `SPEC.md`, frozen as v1.0.
2. `schemas/`: envelope, request, `legality-table-v1`, `profile-v1`.
3. `policy/`: the core table and schemas, and the `1-hive` profile.
4. `fixtures/`.
5. `hiverecord`: the gateway, fold engine, CLI (`hive`) and migrations.
6. The test suites.
7. `docs/OPERATOR_GUIDE.md`, including the adoption paths.
8. `docs/adoption/1-hive.md`.

**R1 and R2 are done when:**
- §22 passes in CI and on a scratch hive of the 1-hive deployment;
- 1-hive runs authoritative for real work;
- the adoption note exists;
- divergences from this spec are recorded as v1 amendments.

---

# Part II — The `1-hive` profile (extension v1)

This part is normative only for hives that pin the `1-hive` profile. It implements 1-hive's operating model:
- humans approve **goals**, not steps;
- one front agent, the **chief of staff**;
- a **supervisor** with a triage agent keeps tasks moving;
- **cost** and **budgets**.

The supervisor's and chief of staff's *behavior* is specified elsewhere (Phase E). This part specifies only what they record.

## 24. Additions

### 24.1 Classes

| Class | Purpose |
|---|---|
| `chief_of_staff` | The human's front agent: opens projects, proposes goals, plans, answers questions, closes and cancels tasks. |
| `supervisor` | Liveness: nudges, restarts, reassigns, escalates. Includes the triage agent. |

### 24.2 Rels and codes

- **Rels:** `goal`, `summary`, `context`, `diagnosis` (`rationale` is a core rel).
- **Codes:** `UNKNOWN_GOAL`, `GOAL_EXISTS`, `GOAL_NOT_ACTIVE`, `GOAL_HAS_OPEN_TASKS`, `BUDGET_RAISE_REQUIRES_OPERATOR`, `INVALID_REVIEW_TARGET`, `NOT_ESCALATED`, `UNKNOWN_PROPOSAL`, `PROPOSAL_EXISTS`, `PROPOSAL_NOT_PENDING`, `PROPOSAL_NOT_APPLICABLE`.

### 24.3 Goals

States: `proposed`, `active`, `completed`, `accepted`, `abandoned`. `accepted` and `abandoned` are terminal.

| Type | Classes | From → To | Data / refs | Conditions |
|---|---|---|---|---|
| `goal.proposed` | chief_of_staff, operator | ∅ → proposed | `project`; `title` (≤200); `objective` (≤1000); `relevance` (≤300, "what remains useless even if all tests pass"); `budget` {usd_micros?, tokens?, wall_clock_seconds?}; pin(goal) | `project_open` |
| `goal.approved` | operator | proposed → active | — | — |
| `goal.revised` | chief_of_staff, operator | proposed, active → same | `reason`; optional `title`, `objective`, `relevance`, `budget`; optional pin(goal) | `budget_not_raised_unless_operator` |
| `goal.completed` | chief_of_staff, operator | active → completed | `outcome` (≤500); pin(summary) | `goal_has_no_open_tasks` |
| `goal.accepted` | operator | completed → accepted | optional `note` | — |
| `goal.reopened` | operator | completed → active | `reason` | — |
| `goal.abandoned` | operator | proposed, active, completed → abandoned | `reason` | — |
| `goal.escalated` | supervisor, chief_of_staff | non-terminal → same | `to` ∈ {chief_of_staff, operator}; `code` ∈ {budget_exceeded, at_risk, other}; `reason`; optional pin(diagnosis) | — |
| `goal.escalation_resolved` | chief_of_staff, operator | same → same | `resolution` | `is_escalated` |

A budget dimension that is absent is unlimited. Abandoning a goal doesn't change its tasks: the chief of staff is expected to cancel them, and the inbox flags abandoned goals with open tasks.

### 24.4 Proposals

States: `pending`, `approved`, `rejected`, `withdrawn`. The last three are terminal.

| Type | Classes; relation | From → To | Data / refs | Conditions |
|---|---|---|---|---|
| `proposal.submitted` | any class except gateway | ∅ → pending | `proposal_id`; `proposed` (a complete request, §7.2); `approver_class` ∈ {operator, chief_of_staff, coordinator}; optional pin(rationale) | `proposed_request_well_formed` |
| `proposal.approved` | operator, chief_of_staff, coordinator | pending → approved | `proposal_id` | `approver_matches`; application (below) |
| `proposal.rejected` | operator, chief_of_staff, coordinator | pending → rejected | `proposal_id`; `reason` | `approver_matches` |
| `proposal.withdrawn` | any; proposer | pending → withdrawn | `proposal_id`; `reason` | — |

**Application.** On `proposal.approved` by actor A, the proposed request is evaluated as if A had submitted it, against the current head: its expected revision (§17.4), then steps 3–7.
- If it's admissible, the gateway appends **two consecutive events atomically**: `proposal.approved`, then the applied event. The applied event copies `type`, `task`, `goal`, `expected_revision`, `refs` and `data` from the proposed request, and sets `actor` = A, `on_proposal` = proposal_id, `via` = the approval's via, `basis` = the approval's basis and `idempotency_key` = `proposal:<proposal_id>`. It has no `signature` (§7.1).
- Otherwise the approval is refused with `PROPOSAL_NOT_APPLICABLE` and `detail.inner_code`, and the proposal stays `pending`. This holds in mirror mode too.

To re-verify an applied event, check that the event before it is a signed `proposal.approved` by the same actor for the same proposal, and that the proposal's `proposed` request matches the copied fields.

### 24.5 Supervision events

| Type | Classes; relation | From → To | Data / refs | Conditions |
|---|---|---|---|---|
| `task.nudged` | supervisor | assigned, in_progress, blocked, in_review → same | `reason`; optional `message` (≤2000); optional pin(diagnosis) | — |
| `task.restarted` | supervisor | assigned, in_progress, blocked → same | `reason`; pin(context); optional pin(diagnosis) | — |
| `task.escalated` | supervisor, chief_of_staff, worker, reviewer; owner | open → same | `to` ∈ {chief_of_staff, operator}; `code` ∈ {repeated_failure, stuck, review_loop, question, other}; `reason`; optional pin(diagnosis) | — |
| `task.escalation_resolved` | chief_of_staff, operator | same → same | `resolution` | `is_escalated` |

### 24.6 Amendments to core rules

| Core event | Amendment |
|---|---|
| `project.opened` | + class chief_of_staff |
| `task.created` | + class chief_of_staff; require envelope `goal`; + `goal_active`; ext: `kind` ∈ {work, review} (default work), `reviews_task`; + `review_task_target_valid` |
| `task.assigned` | + class chief_of_staff; `require_ext: lease` {accept_within_seconds, checkin_every_seconds}; + `review_task_assignee_not_author`; + effect `set_lease` |
| `task.reassigned` | + classes chief_of_staff, supervisor; `require_ext: lease`; + `review_task_assignee_not_author`; + effect `set_lease` |
| `task.answered`, `task.closed`, `review.assigned` | + class chief_of_staff |
| `task.cancelled` | + class chief_of_staff |
| `task.reported`, `task.result_posted`, `review.recorded` | ext: optional `cost` {usd_micros?, tokens?, wall_clock_seconds?}, the cost since the same actor's previous cost report on this task; + effect `add_cost` |
| `project.closed` | + `project_has_no_open_goals` |

**Review tasks.** A task with `ext.kind: review` reviews another task (`ext.reviews_task`), which must be under the same goal and `in_review`. Its result pin is the review document, and it closes like any task. When the chief of staff assigns a review task, it also emits `review.assigned` on the reviewed task, naming the same reviewer. That makes the review task's owner the assigned reviewer. It records the verdict on the reviewed task with `review.recorded`. This lets review work be assigned, supervised and restarted.

## 25. Extension vocabulary

### 25.1 Conditions

| Condition | Holds when | Code |
|---|---|---|
| `project_open` | `data.project` exists and is open | `UNKNOWN_PROJECT`, `PROJECT_NOT_OPEN` |
| `goal_active` | the request's `goal` exists and is `active` | `UNKNOWN_GOAL`, `GOAL_NOT_ACTIVE` |
| `goal_has_no_open_tasks` | no open task under the goal | `GOAL_HAS_OPEN_TASKS` |
| `project_has_no_open_goals` | every goal of the project is terminal | `PROJECT_HAS_OPEN_WORK` |
| `budget_not_raised_unless_operator` | the actor is an operator, or no budget dimension increases or becomes unlimited | `BUDGET_RAISE_REQUIRES_OPERATOR` |
| `is_escalated` | the entity has an open escalation | `NOT_ESCALATED` |
| `review_task_target_valid` | kind=review ⇒ `reviews_task` exists under the same goal and is `in_review` | `INVALID_REVIEW_TARGET` |
| `review_task_assignee_not_author` | kind=review ⇒ the assignee ≠ the author of the reviewed task's current result | `REVIEW_NOT_INDEPENDENT` |
| `proposed_request_well_formed` | `proposed` is a valid request and not itself a proposal | `SCHEMA_INVALID` |
| `approver_matches` | the actor's class = `approver_class`, or `operator` | `NOT_AUTHORIZED` |
| `proposal_applicable` | the proposed request is admissible (§24.4, Application) | `PROPOSAL_NOT_APPLICABLE` |

### 25.2 Effects (extension state only)

- `create_goal`, `mark_goal_approved`, `update_goal_fields`, `record_goal_outcome`;
- `create_proposal`, `decide_proposal`;
- `init_task_ext`, `set_lease` (which also sets `ext.attempt` to 1);
- `increment_attempt` (on `task.restarted`; also counts `restarts`);
- `count_nudge`, and `reset_nudges`, the profile's `on_touch` hook;
- `add_cost` (to the task, and `usd_micros` and `tokens` to its goal);
- `open_escalation`, `close_escalation`, which is also the profile's `on_terminal` hook: reaching a terminal state closes the escalation.

Goal status changes are the rules' `to` states.

### 25.3 State

- **goal:** `id`, `project`, `status`, `status_since`, `title`, `objective`, `relevance`, `budget`, `goal_pin`, `proposed_by`, `proposed_at`, `approved_at`, `outcome`, `summary` (pin), `spent` {usd_micros, tokens} (sum of task cost), `escalation`, `violations`.
- **proposal:** `id`, `proposer`, `approver_class`, `proposed`, `rationale` (pin), `status`, `status_since`, `submitted_at`, `decided_by`, `decided_at`, `reason`.
- **task.ext:** `kind`, `reviews_task`, `lease`, `attempt`, `nudges_since_activity`, `restarts`, `spent` {usd_micros, tokens, wall_clock_seconds}, `escalation`. Tasks created before the profile was pinned get these fields from their first extension effect.
- **escalation:** {to, code, reason, diagnosis (pin), by, position} or null.

Wall-clock spend depends on `now`, so projections compute it. The fold doesn't store it.

## 26. `1-hive` inbox

**For `operator`:**
1. `goal_proposed`: goals in `proposed`, to approve;
2. `goal_completed`: goals in `completed`, to accept or reopen;
3. `proposal_pending`: pending proposals whose `approver_class` is `operator`;
4. `escalation_open`: open escalations `to: operator`, on goals or tasks: budget overrun, repeated failure, and the others;
5. `goal_abandoned_open_tasks`: abandoned goals with open tasks;
6. `exception_pending`: exception requests, to grant or deny. They stay with the human unless the hive has promoted an arbiter.

**For `arbiter`:** `exception_pending`.

**For `chief_of_staff`:**
- `proposal_pending`: pending proposals addressed to it;
- `escalation_open`: escalations `to: chief_of_staff`;
- `task_blocked_unanswered_any`: tasks `blocked` and not answered;
- `goal_active_all_terminal`: `active` goals whose tasks are all terminal (including goals with no tasks yet);
- `task_passed_not_closed`: tasks `in_review` whose current result has a passed review.

The core inbox's operator items move to the chief of staff: in this profile, humans see goals and escalations, not tasks.

## 27. `1-hive` fixtures and tests

**Fixtures** (`fixtures/1-hive/`): `goal-happy-path` (goal → tasks → independent review task → close → goal completed → accepted), `proposals`, `escalations`, `reassign-restart`, `budget`, `refusals` (one per reachable extension code).

**Deliberate-failure cases**, in addition to the core ones:
- a task under a non-active goal;
- the chief of staff approves or accepts a goal;
- the supervisor closes;
- the chief of staff raises a budget;
- a goal completes with open tasks;
- an assignment without a lease;
- a review task assigned to the author;
- approving twice, or by the wrong class;
- an approval after state moved on;
- a nested proposal.

**Property tests:** the core properties (§22.3), plus "every task's goal was `active` at its creation" (in mirror mode: for every `task.created` without an illegal verdict).

---

## Appendix A. Changes from the 1-hive plan (`1-hive/PLAN.md`)

- **One log per hive** with a global position, instead of per-run logs. Actors are hive-wide. Projects group work. Scratch runs become scratch hives. `run.*` events become `hive.*` and `project.*`.
- **Identity is a public key** (draft.3). Requests are signed, and signatures are stored with events. This replaces draft.2's bearer credentials. It matches the Omega architecture (v2.16) and makes the log verifiable by other hives.
- **Review assignment** (draft.3). A coordinator or operator assigns the reviewer for each result, and only that reviewer (or an operator) may record the verdict. Key custody (§6.6) makes this real.
- **Task revision** (draft.3): optional `expected_revision`, `STALE_REVISION`.
- **`gateway.started`** (draft.3) records which implementation made each decision.
- **Core plus profiles.** Goals, proposals, leases, supervision and cost are the `1-hive` extension, not the core, so any hive can adopt the core alone. Extensions may only add or tighten.
- **Review tasks, explicit escalation resolution and `task.answered`** were added. Review tasks and escalations are in the extension; `task.answered` is in the core.

## Appendix B. Changes in draft.4 (2026-09-27), from building the reference implementation

- **Signing** covers the body bytes, and the body must be the canonical request, so any body can be attributed and refused on the record (§6.3). The read signing string is exact.
- **Numbers** are integers only; money is `usd_micros` (§7.3).
- **Bootstrap:** `hive.initialized` registers the first operator, with `keys` (§9, §19.5).
- **Proposal application** defines the applied event's fields, its missing signature and how to re-verify it (§24.4).
- **`goal`** is client-only; the server indexes task events by goal without changing the envelope (§7.1).
- **Registry:** `registry_digest` is defined, and a gateway whose registry differs from the recorded one refuses pins (§8).
- **Mirror mode** mirrors only failures at steps 3, 5 and 6 on mirrorable entities (tasks, goals); existence failures and identity events are always refused (§12.5). The `done` property holds for tasks without violations (§22.3).
- **Table format:** entity kinds, `from` values, the `proposer` relation, hooks and `require_goal` are explicit; `mode_is_mirror` became a from-state; `keys_unused` and `POLICY_INVALID` were added; §13.3 forbids new events that create or move core entities (§11, §13, §15).
- **State** gains `status_since`, event positions in nested records, `task.goal`, `actor.revoked_keys` and more gateway fields (§14.1, §25.3).
- **Inbox rules** are named (§16.2, §26), and the fixture file format is specified (§21).

## Appendix C. Changes in draft.5 (2026-09-28)

- **Exceptions** (§12.6, invariant 10), in answer to Ben's question whether a deterministic ruleset is enough. Rules keep deciding coordination moves; judgment stays with actors (reviewers, triage, chief of staff, humans) whose signed verdicts the rules require. For moves the rules wrongly forbid, a refused request can be admitted exactly as sent after a recorded grant by an operator or `arbiter`. Only waivable codes on exceptable rules can be waived; authentication, authority, pins and review gating never can. Agents can advise in shadow before being promoted to arbiter.
- New core class `arbiter`, entity `exception`, five `exception.*` events, the `requester` relation, the `exceptable` rule flag, and extension fields `forbid_exceptions` and `waivable_codes`.
- **From implementing it** (reference implementation):
  - `rationale` moved from the `1-hive` profile to the core rels, since the core `exception.*` events use it (§8, §24.2).
  - The application condition is named `exception_applicable` (§11.3, §15.1).
  - A grant also requires the refused request's signing key to still be one of the requester's active keys, so every excepted event's signature re-verifies against its actor's key book at that position (inner code `UNKNOWN_KEY`). It also rechecks, under the profile now in force, that the rule is exceptable and the code waivable (`NOT_EXCEPTABLE`), and that the idempotency key is unused (`IDEMPOTENCY_CONFLICT`) (§12.6).
  - The waived code is waived wherever it arises in steps 5–6, not only at its first occurrence (§12.6).
  - The core table lists the core waivable codes as `waivable_codes` (§11.1, §12.6).
  - The `1-hive` inbox also gives `arbiter` its `exception_pending` items, because a profile's inbox replaces the core one (§26).
