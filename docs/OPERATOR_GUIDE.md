# Operator guide

How to run a hive record: set up the database, initialize the hive, run the gateway, add actors, and look after the log. The normative contract is [`SPEC.md`](../SPEC.md). Section numbers below refer to it.

## What you are running

- **Postgres** holds the log in one append-only table. Three roles: `<prefix>_admin` owns the schema and is used only for init and restore; `<prefix>_gateway` may insert and read; `<prefix>_reader` may only read. A trigger blocks `UPDATE`, `DELETE` and `TRUNCATE` for everyone, the owner included (§18).
- **The gateway** (`hive gateway`) is the only writer. It verifies every request's signature, runs the gate against the pinned policy, verifies pins through `hivepin`, and appends. It serves the read API on loopback or a Unix socket (§19).
- **Clients** (`hive emit`, `hive board`, …, or any HTTP client) sign every request with their actor's own ed25519 key. There are no passwords or tokens for actors.

## Prerequisites

- Python ≥ 3.11 and `git` (for `hivepin`).
- Postgres ≥ 14. Rootless Podman works well; `hive scratch up` starts a throwaway one.
- A hivepin **repository registry** listing every repository your pins reference, including the one holding your policy tree (see the hivepin README). Keep it outside this repository.

```bash
uv tool install "git+https://github.com/1-hive/hive-record"     # installs `hive`
```

## 1. Database and roles

As a Postgres superuser, once per hive:

```bash
hive db-bootstrap --superuser-url postgresql://postgres:…@127.0.0.1:5432/postgres \
  --db myhive --prefix myhive --secrets-file ~/.config/hive/myhive-db.json
```

The role passwords are written to the secrets file (mode 0600). Keep it outside every repository. Use `--prefix` to host several hives on one cluster.

## 2. Keys

Each actor creates its key **inside its own sandbox** and hands out only the public key (§6.6):

```bash
hive keygen --out ~/.config/hive/op.key      # prints ed25519:…  (the public key)
```

The private key file never leaves that sandbox. A worker's sandbox must never hold a reviewer's key. The record can't check this; your deployment must (§22.1, "Deployment").

## 3. Pin the policy

The policy is a git tree pinned with R0 (§10). The canonical v1 tree is `policy/` in this repository. Pin it from a published commit:

```bash
hive-pin --json mint hive-record policy > policy.pin
```

You can also publish your own copy. Extensions may only add or tighten (§13.3); the gateway refuses to load one that loosens.

## 4. Initialize

```bash
hive init --admin-url postgresql://myhive_admin:…@127.0.0.1:5432/myhive --prefix myhive \
  --hive myhive --profile core --mode mirror \
  --policy policy.pin --registry ~/.config/hive/registry.json \
  --operator-id alice --operator-key ed25519:…
```

This creates the schema and the generation, and appends `hive.initialized`, which registers the first operator. No secret is created. It refuses a non-empty log.

## 5. Run the gateway

```bash
hive gateway --db-url postgresql://myhive_gateway:…@127.0.0.1:5432/myhive \
  --registry ~/.config/hive/registry.json --port 8470        # or --uds /run/hive/gateway.sock
```

On every start the gateway appends `gateway.started`, recording its version, source commit and registry digest. Bind to loopback or a Unix socket; put TLS in front if you expose it. Run it as a service (e.g. a systemd user unit) with `Restart=on-failure`.

## 6. Use it

Clients read their configuration from the environment:

```bash
export HIVE_URL=http://127.0.0.1:8470 HIVE_ID=myhive HIVE_KEY_FILE=~/.config/hive/alice.key
hive emit actor.registered --data '{"actor_id":"coord","class":"coordinator","role":"planner",
  "keys":["ed25519:…"],"declaration":{"harness":"claude-code","model_route":"default","sandbox":"podman:coord"}}'
hive emit project.opened --data '{"project":"p1","title":"First project"}'
hive board
hive inbox --for operator
hive task t1
hive events --follow
```

For task events, `hive emit` reads the task first and sends its revision as `expected_revision` (§17.4). A refusal prints its code and exits 1; it is also recorded in the log as `gateway.rejected`.

## Adoption paths (§20)

1. **Audit first (R1).** Initialize with `--profile core --mode mirror`. Your agents (or a mirror adapter) write through the API. Illegal task transitions are recorded with `verdict: {legal: false}` instead of refused, and each task counts its `violations`. Identity, schema, pin and existence failures are still refused.
2. **Enforce (R2).** When the board looks right, an operator flips the mode. It can't be flipped back:
   ```bash
   hive emit hive.mode_changed --data '{"mode":"authoritative"}'
   ```
3. **Opt into an extension.** Re-pin the policy with a profile, e.g. `1-hive`:
   ```bash
   hive emit hive.policy_changed --ref policy=policy.pin --data '{"profile":"1-hive","reason":"adopt goals"}'
   ```

Each step is an event in the log.

## Exceptions (§12.6)

When a rule refuses a move that is legitimate (e.g. reopening a task that was closed too early), the refused actor asks for an exception, naming its own refusal:

```bash
hive emit task.reassigned --task t1 --data '{"to":"w2","reason":"closed too early"}'   # refused, ILLEGAL_TRANSITION
hive emit exception.requested --data '{"exception_id":"e1","refusal":"<event_id of that gateway.rejected>","reason":"…"}'
```

An operator or `arbiter` (never the requester) then records `exception.granted` or `exception.denied`. A grant admits the refused request exactly as it was sent, if it is still admissible with that one code waived. Only `ILLEGAL_TRANSITION`, `INVALID_ASSIGNEE`, `NOT_ANSWERED` and `PROJECT_NOT_OPEN` can be waived, and only on task rules other than the review gate, for requests that carried `expected_revision`. Pending requests appear in the `operator` and `arbiter` inboxes. To try an agent as an arbiter, register it as an `instrument` and let it send `exception.advised` first. If the same rule keeps needing exceptions, change the policy instead.

## Changing the registry

The gateway compares its registry file's digest with the recorded `registry_digest`. If they differ, it refuses every pin with `PIN_UNAVAILABLE` (`detail.registry_mismatch`). To change the registry: edit the file, restart the gateway, then have an operator record the new digest:

```bash
python -c 'from hiverecord.pins import registry_digest; print(registry_digest("registry.json"))'
hive emit hive.registry_changed --data '{"registry_digest":"sha256:…","reason":"add the docs repo"}'
```

## Rotating and revoking keys

```bash
hive emit actor.key_added   --data '{"actor_id":"w1","key":"ed25519:NEW"}'          # operator
hive emit actor.key_revoked --data '{"actor_id":"w1","key":"ed25519:OLD","reason":"rotated"}'
```

An actor may revoke its own keys. A revoked key stops working immediately, even for a request already in flight. Retiring an actor (`actor.retired`) disables all of its keys; its id is never reused.

## Backups, export and restore

- **Export** (canonical JSONL, one event per line): `hive export > log.jsonl`, or `hive export --db-url <reader url>`.
- **Verify** an export without any database: `hive verify-log log.jsonl --policy <policy dir>` re-checks every signature and that each key belonged to its actor at that position.
- **Fold** an export without a database: `hive fold log.jsonl --policy <policy dir> [--at N] [--board | --inbox operator]`. The policy directory is checked against the pin's digest, so you need the exact pinned tree (e.g. `hive-pin materialize`).
- **Restore** replaces the log with an export and sets a new **generation**:
  ```bash
  hive restore log.jsonl --admin-url <admin url> --prefix myhive
  ```
  Events after the export are lost, so also run Postgres with WAL archiving if that matters. After a restore, clients holding a cursor get `409 STALE_GENERATION` and must drop it and re-read. The running gateway notices on its next write and reloads; restarting it is cleaner.

## Scratch hives

```bash
hive scratch up              # prints the name, port and superuser URL
hive scratch down --name hive-scratch-…
hive scratch down --all      # removes every scratch container
```

Scratch containers run on tmpfs with `--rm`, so `down` leaves nothing behind.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `401` on every request | Wrong `HIVE_ID`, clock skew over 300 s, or a revoked or unknown key. 401s are logged by the gateway, never recorded. |
| `PIN_INVALID` with `hivepin_code: COMMIT_NOT_PUBLISHED` | The commit isn't reachable from an allowed ref of the registered remote. Push first. |
| `PIN_UNAVAILABLE` | The remote is down (retry), or `registry_mismatch` (see "Changing the registry"). |
| `STALE_REVISION` | Someone changed the task since you read it. Re-read and decide again. |
| `STALE_GENERATION` | The log was restored, or your `basis` is beyond the head. |
| The gateway won't start | The log is empty (run `hive init`), or a pinned policy tree can't be materialized (check the registry and the remote). |
