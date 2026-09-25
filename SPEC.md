# One Hive R1 + R2: The Hive Record — Specification

**Status:** draft v1 for review (not frozen)
**Version:** 1.0-draft.3 · 2026-09-25
**Releases:** R1 (the record), R2 (legality table and review-gated close)
**Depends on:** R0 pinning spec v1.0 (`hivepin` 1.0.0)
**Primary consumers:** any hive adopting the record; the 1-hive operating loop (launcher, supervisor, triage, chief of staff); R3 projections; R5 review harness; R6 worker runtime; R7 messaging; R9 replay.

This document has two parts:
- **Part I, the core.** Normative for every hive. It contains only what the One Hive roadmap has agreed: the record invariants and the M1 task lifecycle.
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

## 6. Identity and authority

### 6.1 Core classes

| Class | Purpose |
|---|---|
| `operator` | A human with final authority. |
| `coordinator` | Creates, assigns, closes and cancels tasks. |
| `worker` | Owns and performs tasks. |
| `reviewer` | Records reviews of results it was assigned to review. |
| `instrument` | Deterministic emitters (metrics, notifier). No task transitions in core. |
| `gateway` | The gateway itself. Emits only `hive.initialized`, `gateway.started` and `gateway.rejected`. |

Extensions may add classes (§13). Each actor has exactly one class. A human who also wants to act as a worker registers a second actor.

### 6.2 Actors

Actor ids match `^[a-z0-9][a-z0-9._-]{0,63}$`. An actor exists from its `actor.registered` event and is `active` until `actor.retired`. A retired actor can't emit anything, and its id is never reused.

An actor's **declaration** records what the actor is: `harness`, `model_route`, `sandbox` (short strings), and optionally a `role_prompt` pin. It is set at registration and replaced by `actor.declared`. Agents MUST re-declare when their configuration changes, including self-modification.

### 6.3 Keys and signed requests

- **Identity is a public key.** Each actor has one or more public keys, recorded in the log (`actor.registered`, `actor.key_added`, `actor.key_revoked`). The fold of those events is the hive's address book: it says which actor a key belongs to. `GET /v1/actors` serves it.
- **Key types are pluggable.** A key is written `<type>:<base64url public key>`. v1 requires `ed25519`. Other types (e.g. Nostr's `secp256k1` Schnorr) can be added in a later version without changing the envelope.
- **Every request is signed.** The client sends:
  - `X-Hive-Key`: the public key;
  - `X-Hive-Signed-At`: RFC 3339 UTC time;
  - `X-Hive-Signature`: base64url signature over the bytes `hive1\n<hive id>\n<signed-at>\n<sha256 hex of the canonical request>`.

  Read requests sign an empty request object `{}` together with the method and path, appended as `\n<METHOD> <path?query>`.
- The gateway rejects a signature whose `signed-at` is more than 300 seconds from its clock. A replayed write is harmless: it has the same idempotency key and digest, so it returns the original event.
- A key is valid iff it has been added and not revoked, and its actor is active. Validity is a function of the fold, so the gateway keeps no separate key store.
- **Nothing secret enters the record.** Private keys never leave the actor's sandbox (§6.6).

### 6.4 Derivation

The gateway sets the envelope's `actor` = `{id, class}` of the key's owner, and stores `signature` = `{key, signed_at, sig}` on the event. The canonical request can be rebuilt from the stored event (§7.2), so anyone holding the log can re-verify every signature. Any `actor` field in a request body is ignored.

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
| `signature` | server | yes, except gateway events | `{key, signed_at, sig}`, as received (§6.4). |
| `via` | server | no | From `X-Hive-Via`. |
| `on_proposal` | server | no | Reserved for extensions (the `1-hive` profile's proposals, §24). |
| `verdict` | server | mirror mode only | `{legal: bool, code?, reason?}` (§12.5). |
| `type` | client | yes | Event type. |
| `task` | client | per type | Task id for task-scoped events. |
| `goal` | client or server | per profile | Reserved for profiles that define goals. For task events the server fills it from the task. |
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

Numbers are integers, or decimals with at most 6 fractional digits. Any other floats are forbidden, so that every implementation produces identical bytes.

### 7.4 Size limits

- The request body is at most 64 KiB.
- `data` canonical bytes are at most 8 KiB.
- Each string has the `maxLength` of its schema. If none is stated, it is 500.

A request over a limit is refused with `PAYLOAD_TOO_LARGE`.

### 7.5 Extension fields

`data.ext` is an optional object reserved for profile extensions. Core schemas allow `ext` only on the event types listed in §9 as "ext allowed"; the active profile's schemas define its contents. With the core profile, `ext` MUST be absent. Core rules and the core fold never read `ext`.

## 8. References and pin admission

Each `refs` entry is `{"rel": <rel>, "pin": <canonical R0 pin object>}`. Unknown fields are refused.

**Core rels:** `order`, `question`, `answer`, `result`, `review`, `report`, `policy`, `role_prompt`. Extensions may add rels.

For every ref, the gateway MUST:
1. check that the rel is required or allowed by the effective rule (`REF_MISSING`, `REF_NOT_ALLOWED`);
2. validate the pin against `pin-v1.schema.json` (`PIN_INVALID`);
3. run `hivepin.verify(pin, publication="required")` against the hive's repository registry. On failure the refusal is `PIN_INVALID` with `detail.hivepin_code`; if the remote can't be reached it is `PIN_UNAVAILABLE`, which is `retryable: true`.

The gateway MAY cache successful publication checks per `(repository, commit_oid)` for the life of its process. Pins MUST NOT be rewritten or normalized: the stored pin is exactly the one submitted.

## 9. Core event catalog

Fields are in `data`. "pin(rel)" means a ref with that rel. Each type has a JSON Schema in the policy tree (§10), and those schemas are authoritative for types and lengths.

| Type | Data | Refs | ext allowed |
|---|---|---|---|
| `hive.initialized` | `mode` ∈ {authoritative, mirror}; `profile` (name); `registry_digest`; `operator` {id, role, declaration} | pin(policy) (a tree pin, §10) | no |
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
| `gateway.started` | `version`; `source_commit`; `policy_digest` | — | no |
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

```jsonc
{
  "schema": "hive.legality/1",
  "version": "1.0.0",
  "rules": [
    {
      "event": "review.recorded",
      "entity": "task",                       // hive | project | actor | task | (extension entities)
      "classes": ["reviewer", "operator"],
      "relation": null,                       // null | "owner" | "self"
      "from": ["in_review"],                  // null means the entity must not exist yet
      "to": {"by": "verdict", "map": {"passed": "in_review", "failed": "in_progress", "needs_information": "in_progress"}},
      "refs": {"required": ["review"], "allowed": []},
      "conditions": ["targets_current_result", "reviewer_not_author", "reviewer_is_assigned"],
      "effects": ["record_review"]
    }
  ]
}
```

- **`to`** is a state name, `"same"`, or `{by, map}`, where the new state is chosen by a `data` field.
- **`relation`** narrows who, within the allowed classes, may emit:
  - `owner`: an actor of class `worker` or `reviewer` must be the task's current owner. Other classes are unaffected.
  - `self`: a non-operator actor must be the subject (`data.actor_id`).
- **Conditions and effects** are names from the fixed vocabularies in §15, optionally with arguments: `{"name": "target_active_class", "args": ["worker", "reviewer"]}`.
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

| Event | Classes; relation | Conditions |
|---|---|---|
| `hive.initialized` | gateway | `log_empty` |
| `hive.policy_changed`, `hive.registry_changed` | operator | — |
| `hive.mode_changed` | operator | `mode_is_mirror` |
| `project.opened` | coordinator, operator | the project doesn't exist |
| `project.closed` | operator | `project_has_no_open_work` |
| `actor.registered` | operator | the id is unused; `class_registrable` |
| `actor.declared` | any class except gateway; self | the actor is active |
| `actor.retired` | operator | the actor is active; `not_last_operator` |
| `actor.key_added` | operator | the actor is active; `key_unused` |
| `actor.key_revoked` | operator; or the actor itself (self) | `key_is_actors` |
| `gateway.started` | gateway | — |

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

`SCHEMA_INVALID`, `UNSUPPORTED_SCHEMA`, `UNKNOWN_EVENT_TYPE`, `PAYLOAD_TOO_LARGE`, `STALE_GENERATION`, `STALE_REVISION`, `IDEMPOTENCY_CONFLICT`, `KEY_EXISTS`, `UNKNOWN_KEY`, `NOT_AUTHORIZED`, `NOT_OWNER`, `UNKNOWN_ACTOR`, `ACTOR_RETIRED`, `ACTOR_EXISTS`, `UNKNOWN_PROJECT`, `PROJECT_EXISTS`, `PROJECT_NOT_OPEN`, `PROJECT_HAS_OPEN_WORK`, `UNKNOWN_TASK`, `TASK_EXISTS`, `ILLEGAL_TRANSITION`, `INVALID_ASSIGNEE`, `NOT_ANSWERED`, `REF_MISSING`, `REF_NOT_ALLOWED`, `PIN_INVALID`, `PIN_UNAVAILABLE`, `REVIEW_REQUIRED`, `REVIEW_STALE`, `REVIEW_NOT_INDEPENDENT`, `REVIEW_NOT_ASSIGNED`, `LAST_OPERATOR`, `INTERNAL_ERROR`.

Only `PIN_UNAVAILABLE` and `INTERNAL_ERROR` are `retryable: true`. Extensions may add codes. Clients MUST rely on codes, not on reason text.

### 12.4 Policy versions

The fold MUST evaluate each event with the profile in force at that event's position. That is the one pinned by `hive.initialized`, or by the latest earlier `hive.policy_changed`.

### 12.5 Modes

- **authoritative:** a failed check refuses the request.
- **mirror:** a request that fails only at steps 3–6 is **appended** with `verdict: {legal: false, code, reason}`. Its `to` state and effects are applied anyway, and the entity's `violations` count goes up by one. Authentication, schema, idempotency and pin failures are still refused.

The mode is set by `hive.initialized`, and the only allowed change is mirror → authoritative. Mirror mode is the release plan's shadow adoption: a hive records its existing flow first, then flips. How a mirror adapter maps a source hive's actors is out of scope for v1.

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
  - `require_ext`: fields of `data.ext` that become mandatory. A missing required field is refused with `SCHEMA_INVALID`.
- `inbox`: inbox rules for this profile (§16.2).

### 13.2 Composition

The effective policy is the core table with every amendment applied, plus the extension's rules. Composition is deterministic, and the result is what the gate and the fold execute.

### 13.3 Add or tighten, never loosen

A valid extension MUST NOT:
- change any core rule's `from`, `to` or `relation`;
- remove, or grant to an existing core class, any permission of a core rule;
- remove any condition or required ref of a core rule;
- define rules for core event types (amendments only);
- use a reserved type prefix.

The gateway refuses to load an extension that breaks any of these. A consequence: every core invariant (§5) holds under every profile. The acceptance suite (§22) runs the core property tests against every shipped profile.

**Promotion:** when a hive has evidence that an extension mechanism works, it can be proposed for the core in a new core version. The rest of One Hive then adopts it, instead of every extension being forced on everyone.

## 14. Fold state

### 14.1 Core state (normative)

- **hive:** `hive`, `mode`, `profile`, `policy` (pin), `registry_digest`, `head`, `gateway` {version, source_commit, since} (from the latest `gateway.started`).
- **actor:** `id`, `class`, `role`, `declaration`, `role_prompt`, `status` ∈ {active, retired}, `keys` (active public keys), `registered_at`.
- **project:** `id`, `title`, `status` ∈ {open, closed}.
- **task:** `id`, `project`, `title`, `order` (pin), `status`, `revision`, `owner`, `assigned_at`, `last_activity` {position, at}, `block` {needs, reason, question, answered, answer} or null, `current_result` {event_id, pin, author, at} or null, `assigned_reviewer` for the current result or null, `latest_review` {event_id, verdict, reviewer, review_pin} for the current result or null, `results_count`, `reviews_failed_count`, `reassignments`, `violations`, `created_by`, `created_at`, `ext` {}.

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
| `mode_is_mirror` | the current mode is `mirror` | `ILLEGAL_TRANSITION` |
| `class_registrable` | `data.class` exists in the profile and ≠ `gateway` | `NOT_AUTHORIZED` |
| `not_last_operator` | the retired actor isn't the last active operator | `LAST_OPERATOR` |
| `project_open_if_given` | `data.project` is absent, or it exists and is open | `PROJECT_NOT_OPEN` |
| `project_has_no_open_work` | no open task (and, per extension, no open goal) in the project | `PROJECT_HAS_OPEN_WORK` |
| `target_active_class(c…)` | `data.to` is an active actor of one of classes `c` | `INVALID_ASSIGNEE` |
| `target_is_not_owner` | `data.to` ≠ the current owner | `INVALID_ASSIGNEE` |
| `answered_if_needed` | the block's `needs` ∈ {access, external}, or a `task.answered` exists since the block | `NOT_ANSWERED` |
| `targets_current_result` | `data.result_event` = the current result | `REVIEW_STALE` |
| `reviewer_not_author` | the actor ≠ the author of the current result | `REVIEW_NOT_INDEPENDENT` |
| `assigned_reviewer_not_author` | `data.reviewer` ≠ the author of the current result | `REVIEW_NOT_INDEPENDENT` |
| `reviewer_is_assigned` | the actor is an operator, or is the reviewer assigned to the current result | `REVIEW_NOT_ASSIGNED` |
| `key_unused` | `data.key` isn't registered to any actor, active or retired | `KEY_EXISTS` |
| `key_is_actors` | `data.key` is an active key of `data.actor_id` | `UNKNOWN_KEY` |
| `current_result_passed` | the latest review of the current result passed | `REVIEW_REQUIRED` |
| *extension conditions* | defined in §25 | per §25 |

### 15.2 Effects

**Core effects** write core state only:
- `create_entity`, `set_owner`, `clear_owner`;
- `record_block`, `record_answer`, `clear_block`;
- `set_current_result`, which also clears the latest review and the assigned reviewer;
- `assign_reviewer`;
- `record_review`, `touch_activity`;
- `register_actor`, `update_declaration`, `retire_actor`;
- `add_key`, `revoke_key`;
- `record_gateway`;
- `set_policy`, `set_registry`, `set_mode`;
- `open_project`, `close_project`.

`touch_activity` is implied for every event by a task's owner on that task.

**Extension effects** write only extension state. They are listed in §25.

## 16. Projections

### 16.1 Board

`hive board [--project] [--at]` renders open tasks by state with owner, last activity and review status (plus extension marks, when the profile defines them). It is a pure rendering of the fold.

### 16.2 Inbox

The inbox is a pure function of the fold and the source of all human notifications. **Core inbox, for `operator`:**
- tasks `blocked` with `needs` ∈ {decision, information} and not answered;
- tasks `in_review` whose current result has a passed review but which are not closed;
- tasks `in_review` with no reviewer assigned to the current result.

Profiles replace or extend the inbox (§26). Each item has a stable key, so notifiers can de-duplicate: `(kind, entity id, position of the causing event)`.

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
- **Restore:** load a dump as `hive_admin`, then replace the generation. Events after the dump are lost. Operators SHOULD run the log with WAL archiving if that matters.

## 19. HTTP API (v1)

JSON bodies (UTF-8). Every response includes `generation` and `head`. Every endpoint except health requires a signed request (§6.3). In v1, every active actor may read everything.

### 19.1 Write

`POST /v1/events`, with a request body (§7.2).
- `201 {"status":"accepted","event":…}` on admission.
- `200 {"status":"duplicate","event":…}` for an idempotent repeat.
- `422 {"status":"refused","code","reason","retryable","refusal":<event>}` for a recorded refusal.
- `401` unauthenticated (not recorded); `413` over the request limit (recorded); `409` stale generation (recorded).

If an extension defines events that append more than one event atomically (e.g. proposal approval, §24), the response includes all of them in `events`.

### 19.2 Read

- `GET /v1/events?after=&limit=(≤1000)&type=&task=&goal=`.
- `GET /v1/subscribe?after=`: Server-Sent Events with `id:` = position, resumable with `Last-Event-ID`.
- `GET /v1/state?at=`: the fold at a position (default: head).
- `GET /v1/tasks/{id}`: the task's state plus its event history. Extensions add entity endpoints, e.g. `/v1/goals/{id}`.
- `GET /v1/inbox?for=<class>`.
- `GET /v1/actors`: the address book (actors, classes, active keys, declarations).

### 19.3 Health

`GET /v1/health` (no authentication) → `{status, hive, head, generation, profile, policy_version, gateway_version}`.

On every start, the gateway appends `gateway.started` with its version and source commit before admitting any request. This makes it possible to tell which implementation made each decision.

### 19.4 Keys

Keys are public, so there is no special endpoint: an operator adds or revokes keys with ordinary `actor.key_added` and `actor.key_revoked` events. `hive keygen` creates an ed25519 key pair inside the calling sandbox and prints only the public key.

### 19.5 Bootstrap

`hive init --hive <id> --profile <name> --policy <pin> --mode <mode>` runs as `hive_admin`. It takes the first operator's **public** key (`--operator-key`). It creates the schema and generation, and has the gateway append `hive.initialized` and `actor.registered` for the first operator. No secret is created or printed. It refuses a non-empty log.

## 20. Adoption paths

1. **Audit first (R1):** `core` profile in `mirror` mode. A mirror adapter or the hive's own agents write through the API, and nothing is refused. The hive gets an audit trail, history and a board.
2. **Enforce (R2):** flip to `authoritative`. Illegal transitions and unreviewed closes are refused.
3. **Opt into extensions:** re-pin the policy with an extension profile, e.g. `1-hive`, via `hive.policy_changed`.

Each step is a recorded event, and no step requires the operating loop of any other hive.

## 21. Conformance fixtures

`fixtures/<profile>/<name>/` contains:
- `log.jsonl`;
- `policy/`, or a reference to the pinned tree;
- `expected/state@<pos>.json`;
- `expected/inbox@<pos>.json`;
- `requests.jsonl` plus `expected/responses.jsonl`, replayed against a scratch gateway.

Pins reference a fixture repository created by the deterministic `fixtures/make-repo.sh`.

**Core fixtures:** `happy-path`, `review-loop`, `stale-review`, `block-answer`, `reassign`, `policy-change`, `mirror-mode`, `mode-flip`, and `refusals` (one per reachable core code).

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

### 22.3 Properties, run under every shipped profile

Random sequences of well-formed requests from random actors, in both modes, check:
- `done` ⇒ the latest review of the current result passed, its reviewer ≠ the author, and the reviewer was assigned to that result or is an operator;
- in authoritative mode, no appended event failed its guard;
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

- **Rels:** `goal`, `summary`, `context`, `diagnosis`, `rationale`.
- **Codes:** `UNKNOWN_GOAL`, `GOAL_EXISTS`, `GOAL_NOT_ACTIVE`, `GOAL_HAS_OPEN_TASKS`, `BUDGET_RAISE_REQUIRES_OPERATOR`, `INVALID_REVIEW_TARGET`, `NOT_ESCALATED`, `UNKNOWN_PROPOSAL`, `PROPOSAL_EXISTS`, `PROPOSAL_NOT_PENDING`, `PROPOSAL_NOT_APPLICABLE`.

### 24.3 Goals

States: `proposed`, `active`, `completed`, `accepted`, `abandoned`. `accepted` and `abandoned` are terminal.

| Type | Classes | From → To | Data / refs | Conditions |
|---|---|---|---|---|
| `goal.proposed` | chief_of_staff, operator | ∅ → proposed | `project`; `title` (≤200); `objective` (≤1000); `relevance` (≤300, "what remains useless even if all tests pass"); `budget` {usd?, tokens?, wall_clock_seconds?}; pin(goal) | `project_open` |
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

**Application.** On `proposal.approved` by actor A, the proposed request is evaluated through the full gate (steps 3–7) as if A had submitted it, against the current head.
- If it's admissible, the gateway appends **two consecutive events atomically**: `proposal.approved`, then the applied event, with `actor` = A, `on_proposal` = proposal_id, `via` = the approval's via, and `idempotency_key` = `proposal:<proposal_id>`.
- Otherwise the approval is refused with `PROPOSAL_NOT_APPLICABLE` and `detail.inner_code`, and the proposal stays `pending`.

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
| `task.reported`, `task.result_posted`, `review.recorded` | ext: optional `cost` {usd?, tokens?, wall_clock_seconds?}, the cost since the same actor's previous cost report on this task; + effect `add_cost` |
| `project.closed` | + `project_has_no_open_goals` |

**Review tasks.** A task with `ext.kind: review` reviews another task (`ext.reviews_task`), which must be under the same goal and `in_review`. Its result pin is the review document, and it closes like any task. When the chief of staff assigns a review task, it also emits `review.assigned` on the reviewed task, naming the same reviewer. That makes the review task's owner the assigned reviewer. It records the verdict on the reviewed task with `review.recorded`. This lets review work be assigned, supervised and restarted.

## 25. Extension vocabulary

### 25.1 Conditions

| Condition | Holds when | Code |
|---|---|---|
| `project_open` | `data.project` exists and is open | `PROJECT_NOT_OPEN` |
| `goal_active` | the request's `goal` exists and is `active` | `GOAL_NOT_ACTIVE` |
| `goal_has_no_open_tasks` | no open task under the goal | `GOAL_HAS_OPEN_TASKS` |
| `project_has_no_open_goals` | every goal of the project is terminal | `PROJECT_HAS_OPEN_WORK` |
| `budget_not_raised_unless_operator` | the actor is an operator, or no budget dimension increases or becomes unlimited | `BUDGET_RAISE_REQUIRES_OPERATOR` |
| `is_escalated` | the entity has an open escalation | `NOT_ESCALATED` |
| `review_task_target_valid` | kind=review ⇒ `reviews_task` exists under the same goal and is `in_review` | `INVALID_REVIEW_TARGET` |
| `review_task_assignee_not_author` | kind=review ⇒ the assignee ≠ the author of the reviewed task's current result | `REVIEW_NOT_INDEPENDENT` |
| `proposed_request_well_formed` | `proposed` is a valid request and not itself a proposal | `SCHEMA_INVALID` |
| `approver_matches` | the actor's class = `approver_class`, or `operator` | `NOT_AUTHORIZED` |

### 25.2 Effects (extension state only)

- `create_goal`, `update_goal_fields`, `set_goal_status`;
- `create_proposal`, `decide_proposal`;
- `set_lease`, which also resets `ext.attempt` to 1;
- `increment_attempt` (on `task.restarted`);
- `count_nudge` (reset by `touch_activity`);
- `add_cost`;
- `open_escalation`, `close_escalation`, where reaching a terminal state also closes the escalation.

### 25.3 State

- **goal:** `id`, `project`, `status`, `title`, `objective`, `relevance`, `budget`, `goal_pin`, `proposed_by`, `approved_at`, `spent` {usd, tokens} (sum of task cost), `escalation`, `violations`.
- **proposal:** `id`, `proposer`, `approver_class`, `proposed`, `status`, `submitted_at`, `decided_by`, `decided_at`.
- **task.ext:** `kind`, `reviews_task`, `lease`, `attempt`, `nudges_since_activity`, `restarts`, `spent` {usd, tokens, wall_clock_seconds}, `escalation`.

Wall-clock spend depends on `now`, so projections compute it. The fold doesn't store it.

## 26. `1-hive` inbox

**For `operator`:**
1. goals in `proposed`, to approve;
2. goals in `completed`, to accept or reopen;
3. pending proposals whose `approver_class` is `operator`;
4. open escalations `to: operator`, on goals or tasks: budget overrun, repeated failure, and the others;
5. abandoned goals with open tasks.

**For `chief_of_staff`:**
- pending proposals addressed to it;
- escalations `to: chief_of_staff`;
- tasks `blocked` and not answered;
- `active` goals whose tasks are all terminal;
- tasks `in_review` whose current result has a passed review.

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

**Property tests:** the core properties (§22.3), plus "every task's goal was `active` at its creation".

---

## Appendix A. Changes from PLAN.md

- **One log per hive** with a global position, instead of per-run logs. Actors are hive-wide. Projects group work. Scratch runs become scratch hives. `run.*` events become `hive.*` and `project.*`.
- **Identity is a public key** (draft.3). Requests are signed, and signatures are stored with events. This replaces draft.2's bearer credentials. It matches the Omega architecture (v2.16) and makes the log verifiable by other hives.
- **Review assignment** (draft.3). A coordinator or operator assigns the reviewer for each result, and only that reviewer (or an operator) may record the verdict. Key custody (§6.6) makes this real.
- **Task revision** (draft.3): optional `expected_revision`, `STALE_REVISION`.
- **`gateway.started`** (draft.3) records which implementation made each decision.
- **Core plus profiles.** Goals, proposals, leases, supervision and cost are the `1-hive` extension, not the core, so any hive can adopt the core alone. Extensions may only add or tighten.
- **Review tasks, explicit escalation resolution and `task.answered`** were added. Review tasks and escalations are in the extension; `task.answered` is in the core.
