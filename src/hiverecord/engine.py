# SPDX-License-Identifier: GPL-3.0-or-later
"""The gate and the fold (SPEC §12, §14).

One :class:`Engine` holds the fold of a log prefix and the policy in force. The
gate evaluates a request against it; the fold applies admitted events. Both read
the same :class:`~hiverecord.policy.Rule`, so an admitted event changes exactly
what its rule says (invariant 5). The engine does no I/O: pin verification and
policy resolution are injected.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from hivepin import Pin, PinError

from . import vocab
from .canonical import EncodingError, Ids, digest, dumps, format_time, loads, sha256_hex, utcnow
from .errors import Refusal
from .policy import GATEWAY_TYPES, Policy, PolicyError, meta_validator, schema_errors

REQUEST_FIELDS = ("type", "task", "goal", "basis", "expected_revision", "idempotency_key", "refs", "data")
MAX_BODY = 64 * 1024
MAX_DATA = 8 * 1024
GATEWAY_ACTOR = {"id": "gateway", "class": "gateway"}

PolicyResolver = Callable[[dict, str], Policy]
PinVerifier = Callable[[dict], None]


class LogError(Exception):
    """A log that no conforming gateway could have written."""


@dataclass
class Auth:
    actor: dict                 # {id, class}
    signature: dict | None      # {key, signed_at, sig}


@dataclass
class Outcome:
    status: str                 # accepted | duplicate | refused
    events: list[dict] = field(default_factory=list)
    refusal: Refusal | None = None

    @property
    def event(self) -> dict:
        return self.events[0]

    @property
    def http(self) -> int:
        """SPEC §19.1."""
        if self.status == "accepted":
            return 201
        if self.status == "duplicate":
            return 200
        return {"PAYLOAD_TOO_LARGE": 413, "STALE_GENERATION": 409}.get(self.refusal.code, 422)


def empty_state() -> dict:
    return {"hive": None, "actors": {}, "projects": {}, "tasks": {}, "exceptions": {}}


def request_of(event: dict) -> dict:
    """The canonical request an event was admitted from (SPEC §7.2)."""
    return {k: event[k] for k in REQUEST_FIELDS if k in event}


def request_digest(req: dict) -> str:
    return sha256_hex(dumps(req))


class Engine:
    def __init__(self, resolver: PolicyResolver, *, pins: PinVerifier | None = None,
                 registry_digest: str | None = None, clock: Callable = utcnow,
                 ids: Ids | None = None) -> None:
        self.resolver = resolver
        self.pins = pins
        self.registry_digest = registry_digest
        self.clock = clock
        self.ids = ids or Ids()
        self.state = empty_state()
        self.policy: Policy | None = None
        self.events: list[dict] = []
        self.idem: dict[tuple[str, str], tuple[int, str]] = {}
        self.by_id: dict[str, int] = {}
        self._inner: dict | None = None

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #
    @property
    def head(self) -> int:
        return self.state["hive"]["head"] if self.state["hive"] else 0

    @property
    def hive_id(self) -> str | None:
        return self.state["hive"]["hive"] if self.state["hive"] else None

    @property
    def mode(self) -> str | None:
        return self.state["hive"]["mode"] if self.state["hive"] else None

    def actor_for_key(self, key: str) -> dict | None:
        """The address book (SPEC §6.3): the active actor holding an active key."""
        for a in self.state["actors"].values():
            if a["status"] == "active" and key in a["keys"]:
                return {"id": a["id"], "class": a["class"]}
        return None

    def event_by_id(self, event_id: object) -> dict | None:
        i = self.by_id.get(event_id) if isinstance(event_id, str) else None
        return None if i is None else self.events[i]

    def state_bytes(self) -> bytes:
        return dumps(self.state)

    # ------------------------------------------------------------------ #
    # fold
    # ------------------------------------------------------------------ #
    def switch_policy(self, pin: dict, profile: str) -> None:
        self.policy = self.resolver(pin, profile)

    def replay(self, events: Iterable[dict]) -> None:
        for ev in events:
            self.apply(ev)

    def apply(self, ev: dict) -> None:
        """Fold one event. Raises LogError on a log no gateway could have written."""
        pos = ev.get("position")
        if pos != self.head + 1:
            raise LogError(f"position {pos} after head {self.head}")
        t = ev["type"]
        if t == "hive.initialized":
            pins = [r["pin"] for r in ev["refs"] if r["rel"] == "policy"]
            if not pins:
                raise LogError("hive.initialized without a policy pin")
            self.policy = self.resolver(pins[0], ev["data"]["profile"])
        if self.policy is None:
            raise LogError("the first event must be hive.initialized")
        rule = self.policy.rules.get(t)
        if rule is None:
            raise LogError(f"position {pos}: {t} is not in the profile in force")
        self.events.append(ev)
        self.by_id[ev["event_id"]] = len(self.events) - 1
        if t != "gateway.rejected":
            try:
                self._fold(ev, rule)
            except (KeyError, TypeError) as exc:
                raise LogError(f"position {pos}: cannot fold {t}: {exc!r}") from None
            self.idem[(ev["actor"]["id"], ev["idempotency_key"])] = (
                len(self.events) - 1, request_digest(request_of(ev)))
        self.state["hive"]["head"] = pos

    def _fold(self, ev: dict, rule) -> None:
        policy = self.policy
        kind = rule.entity
        edef = policy.entities[kind]
        sf = edef["state_field"]
        ent_id = self._entity_id(edef, ev)
        to = rule.to
        if rule.frm is None:
            new = to["map"][ev["data"][to["by"]]] if isinstance(to, dict) else to
            if kind == "hive":
                entity = {"mode": new, "head": 0}
                self.state["hive"] = entity
            else:
                entity = {"id": ent_id, sf: new, "status_since": ev["position"]}
                if edef["mirrorable"]:
                    entity["violations"] = 0
                coll = self.state.setdefault(edef["collection"], {})
                if ent_id in coll:
                    raise LogError(f"{kind} {ent_id} already exists")
                coll[ent_id] = entity
            old = None
        else:
            entity = self._lookup(edef, ent_id)
            if entity is None:
                raise LogError(f"unknown {kind} {ent_id!r}")
            old = entity[sf]
            if isinstance(to, dict):
                new = to["map"][ev["data"][to["by"]]]
            else:
                new = old if to == "same" else to
            if new != old:
                entity[sf] = new
                if "status_since" in entity:
                    entity["status_since"] = ev["position"]

        pre_owner = entity.get("owner") if kind == "task" else None
        ctx = vocab.Ctx(state=self.state, policy=policy, rule=rule, req=ev, actor=ev["actor"],
                        entity=entity, event=ev, engine=self)
        for name in rule.effects:
            vocab.EFFECTS[name][1](ctx)

        if "on_exception" in ev:
            exc = self.state["exceptions"][ev["on_exception"]]
            exc["applied_event"] = {"event_id": ev["event_id"], "position": ev["position"]}
            if kind == "task":
                entity["exceptions_applied"] = entity.get("exceptions_applied", 0) + 1
        verdict = ev.get("verdict")
        if verdict and not verdict["legal"] and "violations" in entity:
            entity["violations"] += 1
        if kind == "task":
            entity["revision"] = entity.get("revision", 0) + 1
            if pre_owner is not None and ev["actor"]["id"] == pre_owner:
                entity["last_activity"] = {"position": ev["position"], "at": ev["recorded_at"]}
                for name in policy.hooks.get("on_touch", ()):
                    vocab.EFFECTS[name][1](ctx)
        if old not in edef["terminal"] and new in edef["terminal"]:
            for name in policy.hooks.get("on_terminal", ()):
                vocab.EFFECTS[name][1](ctx)

    def _entity_id(self, edef: dict, req: dict) -> str | None:
        src = edef["id"]
        if src is None:
            return None
        if src.startswith("data."):
            return req.get("data", {}).get(src[5:])
        return req.get(src)

    def _lookup(self, edef: dict, ent_id: str | None) -> dict | None:
        if edef["collection"] is None:
            return self.state["hive"]
        return self.state.get(edef["collection"], {}).get(ent_id)

    # ------------------------------------------------------------------ #
    # gate
    # ------------------------------------------------------------------ #
    def submit(self, body: bytes, auth: Auth, *, via: str | None = None,
               generation_ok: bool = True) -> Outcome:
        """Evaluate a signed request and append what it produces: its event(s),
        or one gateway.rejected. ``auth`` has already been verified (SPEC §6.4)."""
        req: object = None
        try:
            if len(body) > MAX_BODY:
                raise Refusal("PAYLOAD_TOO_LARGE", f"body is {len(body)} bytes (max {MAX_BODY})")
            try:
                req = loads(body)
            except EncodingError as exc:
                raise Refusal("SCHEMA_INVALID", str(exc)) from None
            if not isinstance(req, dict):
                req = None
                raise Refusal("SCHEMA_INVALID", "the request must be a JSON object")
            if dumps(req) != body:
                raise Refusal("SCHEMA_INVALID", "the body is not in canonical form")
            rule = self._structure(req)
            if not generation_ok:
                raise Refusal("STALE_GENERATION", "stale X-Hive-Generation")
            if req["basis"] > self.head:
                raise Refusal("STALE_GENERATION", f"basis {req['basis']} is beyond head {self.head}")
            prior = self.idem.get((auth.actor["id"], req["idempotency_key"]))
            if prior is not None:
                idx, dig = prior
                if dig == sha256_hex(body):
                    return Outcome("duplicate", [self.events[idx]])
                raise Refusal("IDEMPOTENCY_CONFLICT", "idempotency key reused with a different request")
            self._check_revision(req)
            self._inner = None
            verdict = self._guard(req, auth.actor, rule, mirror=self.mode == "mirror")
            self._verify_refs(req)
        except Refusal as r:
            if r.code not in self.policy.codes:
                r = Refusal("INTERNAL_ERROR", f"undeclared code {r.code}: {r.reason}")
            return Outcome("refused", [self._reject(r, auth, via, body, req)], r)

        inner, self._inner = self._inner, None
        out = [self._append(req, auth.actor, signature=auth.signature, via=via, verdict=verdict)]
        if inner is not None and inner["kind"] == "proposal":
            proposal = inner["entity"]
            fields = {k: proposal["proposed"][k] for k in ("type", "task", "goal", "expected_revision",
                                                           "refs", "data") if k in proposal["proposed"]}
            fields.update(basis=req["basis"], idempotency_key=f"proposal:{proposal['id']}")
            out.append(self._append(fields, auth.actor, signature=None, via=via, verdict=None,
                                    extra={"on_proposal": proposal["id"]}))
        elif inner is not None:
            # SPEC §12.6: the refused request exactly, under its requester and signature
            refusal = inner["refusal"]
            out.append(self._append(refusal["data"]["refused"], inner["requester"],
                                    signature=refusal.get("signature"), via=refusal["data"].get("via"),
                                    verdict=None, extra={"on_exception": inner["entity"]["id"]}))
        return Outcome("accepted", out)

    def append_gateway(self, type_: str, data: dict, refs: list | None = None) -> dict:
        """Append an event whose actor is the gateway (hive.initialized, gateway.started)."""
        req = {"type": type_, "basis": self.head, "idempotency_key": f"gateway:{self.head + 1}",
               "refs": refs or [], "data": data}
        if self.policy is not None:
            self._structure(req)
            self._guard(req, GATEWAY_ACTOR, self.policy.rules[type_], mirror=False)
            self._verify_refs(req)
        return self._append(req, GATEWAY_ACTOR, signature=None, via=None, verdict=None)

    def initialize(self, *, hive: str, mode: str, profile: str, policy_pin: dict,
                   registry_digest: str, operator: dict) -> dict:
        """Append hive.initialized (SPEC §19.5). The log must be empty."""
        if self.head:
            raise Refusal("ILLEGAL_TRANSITION", "the log is not empty")
        self._hive_for_init = hive
        self.policy = self.resolver(policy_pin, profile)
        req = {"type": "hive.initialized", "basis": 0, "idempotency_key": "gateway:1",
               "refs": [{"rel": "policy", "pin": policy_pin}],
               "data": {"mode": mode, "profile": profile, "registry_digest": registry_digest,
                        "operator": operator}}
        self._structure(req)
        self._guard(req, GATEWAY_ACTOR, self.policy.rules["hive.initialized"], mirror=False)
        self._verify_refs(req)
        try:
            return self._append(req, GATEWAY_ACTOR, signature=None, via=None, verdict=None)
        finally:
            del self._hive_for_init

    # --- the steps ------------------------------------------------------
    def _structure(self, req: dict) -> object:
        """Step 1 after parsing: request shape, type, size, data and ext schemas."""
        err = schema_errors(meta_validator("request-v1.schema.json"), req)
        if err:
            raise Refusal("SCHEMA_INVALID", err)
        t = req["type"]
        rule = self.policy.rules.get(t)
        if rule is None:
            raise Refusal("UNKNOWN_EVENT_TYPE", f"{t} is not in profile {self.policy.name}")
        data = req["data"]
        if len(dumps(data)) > MAX_DATA:
            raise Refusal("PAYLOAD_TOO_LARGE", f"data exceeds {MAX_DATA} bytes")
        err = schema_errors(self.policy.data_schemas[t], data)
        if err:
            raise Refusal("SCHEMA_INVALID", f"data: {err}")
        if "ext" in data:
            ext_v = self.policy.ext_schemas.get(t)
            if ext_v is None:
                raise Refusal("SCHEMA_INVALID", f"data.ext is not allowed on {t} in profile {self.policy.name}")
            err = schema_errors(ext_v, data["ext"])
            if err:
                raise Refusal("SCHEMA_INVALID", f"data.ext: {err}")
        for f in rule.require_ext:
            if f not in data.get("ext", {}):
                raise Refusal("SCHEMA_INVALID", f"data.ext.{f} is required")
        if (rule.entity == "task") != ("task" in req):
            raise Refusal("SCHEMA_INVALID", "task is required" if rule.entity == "task" else "task is not allowed")
        needs_goal = rule.entity == "goal" or rule.require_goal
        if needs_goal != ("goal" in req):
            raise Refusal("SCHEMA_INVALID", "goal is required" if needs_goal else "goal is not allowed")
        if "expected_revision" in req and "task" not in req:
            raise Refusal("SCHEMA_INVALID", "expected_revision applies to task events only")
        return rule

    def _check_revision(self, req: dict) -> None:
        if "expected_revision" not in req:
            return
        task = self.state["tasks"].get(req["task"])
        if task is not None and task["revision"] != req["expected_revision"]:
            raise Refusal("STALE_REVISION",
                          f"task {task['id']} is at revision {task['revision']}, not {req['expected_revision']}")

    def _guard(self, req: dict, actor: dict, rule, *, mirror: bool, waive: str | None = None) -> dict | None:
        """Steps 3-6. Returns the mirror-mode verdict, or None in authoritative mode.
        ``waive`` is the one waivable code an exception grant waives (SPEC §12.6)."""
        policy = self.policy
        kind = rule.entity
        edef = policy.entities[kind]
        ent_id = self._entity_id(edef, req)
        entity = self._lookup(edef, ent_id)
        lenient = mirror and edef["mirrorable"]
        soft: list[Refusal] = []
        if waive is not None and waive not in policy.waivable:
            raise Refusal("INTERNAL_ERROR", f"{waive} is not waivable")

        def fail(r: Refusal) -> None:
            if not lenient:
                raise r
            soft.append(r)

        def waived(r: Refusal) -> bool:
            return waive is not None and r.code == waive

        # step 3: class, then relation
        if not rule.permits(actor["class"]):
            fail(Refusal("NOT_AUTHORIZED", f"class {actor['class']} may not emit {rule.event}"))
        elif rule.relation == "owner" and actor["class"] in ("worker", "reviewer"):
            if entity is not None and entity.get("owner") != actor["id"]:
                fail(Refusal("NOT_OWNER", f"{actor['id']} does not own task {ent_id}"))
        elif rule.relation == "self" and actor["class"] != vocab.OPERATOR:
            if req["data"].get("actor_id") != actor["id"]:
                fail(Refusal("NOT_OWNER", "only the actor itself (or an operator) may do this"))
        elif (rule.relation == "proposer" and actor["class"] != vocab.OPERATOR
              and entity is not None and entity.get("proposer") != actor["id"]):
            fail(Refusal("NOT_OWNER", "only the proposer (or an operator) may do this"))
        elif (rule.relation == "requester" and actor["class"] != vocab.OPERATOR
              and entity is not None and entity.get("requester") != actor["id"]):
            fail(Refusal("NOT_OWNER", "only the requester (or an operator) may do this"))

        # step 4: existence (never lenient)
        if rule.frm is None:
            if entity is not None:
                raise Refusal(edef["exists_code"], f"{kind} {ent_id!r} already exists")
        elif entity is None:
            raise Refusal(edef["unknown_code"], f"unknown {kind} {ent_id!r}")

        # step 5: from-state
        if not soft and rule.frm not in (None, "*"):
            state = entity[edef["state_field"]]
            ok = state not in edef["terminal"] if rule.frm == "open" else state in rule.frm
            r = Refusal(edef["illegal_code"], f"{rule.event} is not allowed from {state}")
            if not ok and not waived(r):
                fail(r)

        # step 6: refs, then conditions
        if not soft:
            rels = [r["rel"] for r in req["refs"]]
            missing = [r for r in rule.required if r not in rels]
            extra = [r for r in rels if r not in rule.required + rule.allowed]
            if missing:
                fail(Refusal("REF_MISSING", f"missing ref {missing[0]}"))
            elif extra or len(set(rels)) != len(rels):
                fail(Refusal("REF_NOT_ALLOWED", f"ref {(extra or rels)[0]} is not allowed here"))
        if not soft:
            for name, args in rule.conditions:
                ctx = vocab.Ctx(state=self.state, policy=policy, rule=rule, req=req, actor=actor,
                                entity=entity, args=args, engine=self)
                try:
                    vocab.CONDITIONS[name][0](ctx)
                except Refusal as r:
                    if r.code in ("PROPOSAL_NOT_APPLICABLE", "EXCEPTION_NOT_APPLICABLE"):
                        raise
                    if waived(r):
                        continue
                    fail(r)
                    break

        if not mirror:
            return None
        if soft:
            return {"legal": False, "code": soft[0].code, "reason": soft[0].reason}
        return {"legal": True}

    def _verify_refs(self, req: dict) -> None:
        """Step 7: pin verification (network), and policy loading for policy pins."""
        for ref in req["refs"]:
            pin = ref["pin"]
            try:
                parsed = Pin.from_dict(pin)
            except PinError as exc:
                raise Refusal("PIN_INVALID", exc.message, hivepin_code=exc.code) from None
            if parsed.to_canonical_dict() != pin:
                raise Refusal("PIN_INVALID", "the pin is not a canonical R0 pin object")
            hive = self.state["hive"]
            if hive and self.registry_digest and hive["registry_digest"] != self.registry_digest:
                raise Refusal("PIN_UNAVAILABLE", "the gateway's registry differs from the recorded one",
                              registry_mismatch=True)
            if self.pins is not None:
                self.pins(pin)
        if req["type"] in ("hive.initialized", "hive.policy_changed"):
            pin = next(r["pin"] for r in req["refs"] if r["rel"] == "policy")
            try:
                self.resolver(pin, req["data"]["profile"])
            except (PolicyError, PinError, OSError) as exc:
                raise Refusal("POLICY_INVALID", str(exc)[:500]) from None

    # --- proposals (SPEC §24.4) --------------------------------------------
    def check_proposed(self, proposed: object) -> tuple[str | None, str]:
        if not isinstance(proposed, dict):
            return "SCHEMA_INVALID", "not an object"
        t = proposed.get("type")
        if isinstance(t, str) and (t.startswith("proposal.") or t in GATEWAY_TYPES):
            return "SCHEMA_INVALID", f"{t} cannot be proposed"
        try:
            self._structure(proposed)
        except Refusal as r:
            return r.code, r.reason
        return None, ""

    def check_application(self, ctx: vocab.Ctx) -> None:
        proposal = ctx.entity
        if proposal is None or proposal["status"] != "pending":
            return
        inner = proposal["proposed"]
        actor = ctx.actor
        try:
            code, reason = self.check_proposed(inner)
            if code:
                raise Refusal(code, reason)
            if (actor["id"], f"proposal:{proposal['id']}") in self.idem:
                raise Refusal("IDEMPOTENCY_CONFLICT", "the approver already used this key")
            self._check_revision(inner)
            self._guard(inner, actor, self.policy.rules[inner["type"]], mirror=False)
            self._verify_refs(inner)
        except Refusal as r:
            raise Refusal("PROPOSAL_NOT_APPLICABLE", f"the proposed {inner.get('type')} is not admissible: {r.reason}",
                          inner_code=r.code, **r.detail) from None
        self._inner = {"kind": "proposal", "entity": proposal}

    # --- exceptions (SPEC §12.6) ------------------------------------------------
    def check_exception(self, ctx: vocab.Ctx) -> None:
        exc = ctx.entity
        if exc is None or exc["status"] != "pending":
            return
        refusal = self.event_by_id(exc["refusal"]["event_id"])
        req = refusal["data"]["refused"]
        code = exc["refusal"]["code"]
        by = refusal["data"]["by"]
        try:
            requester = self.state["actors"].get(by["id"])
            if requester is None or requester["status"] != "active":
                raise Refusal("ACTOR_RETIRED", f"the requester {by['id']} is no longer active")
            sig = refusal.get("signature")
            if not sig or sig["key"] not in requester["keys"]:
                raise Refusal("UNKNOWN_KEY", "the refused request's key is no longer the requester's")
            actor = {"id": requester["id"], "class": requester["class"]}
            if (actor["id"], req["idempotency_key"]) in self.idem:
                raise Refusal("IDEMPOTENCY_CONFLICT", "the request's idempotency key has since been used")
            rule = self._structure(req)
            if req["basis"] > self.head:
                raise Refusal("STALE_GENERATION", "basis beyond head")
            if code not in self.policy.waivable or not rule.exceptable:
                raise Refusal("NOT_EXCEPTABLE", f"{code} on {rule.event} can no longer be waived")
            self._check_revision(req)
            self._guard(req, actor, rule, mirror=False, waive=code)
            self._verify_refs(req)
        except Refusal as r:
            raise Refusal("EXCEPTION_NOT_APPLICABLE", f"the refused {req.get('type')} is still not admissible: {r.reason}",
                          inner_code=r.code, **r.detail) from None
        self._inner = {"kind": "exception", "entity": exc, "refusal": refusal, "requester": actor}

    # --- appending ----------------------------------------------------------
    def _append(self, req: dict, actor: dict, *, signature: dict | None, via: str | None,
                verdict: dict | None, extra: dict | None = None) -> dict:
        now = self.clock()
        hive = self.hive_id or getattr(self, "_hive_for_init", None)
        ev = {"schema": "hive.event/1", "hive": hive, "position": self.head + 1,
              "event_id": self.ids.uuid7(now), "recorded_at": format_time(now),
              "actor": dict(actor), **{k: copy.deepcopy(req[k]) for k in REQUEST_FIELDS if k in req}}
        if signature is not None:
            ev["signature"] = dict(signature)
        if via:
            ev["via"] = via
        ev.update(extra or {})
        if verdict is not None:
            ev["verdict"] = verdict
        self.apply(ev)
        return ev

    def _reject(self, r: Refusal, auth: Auth, via: str | None, body: bytes, req: object) -> dict:
        refused = req if isinstance(req, dict) and len(body) <= MAX_BODY else None
        data = {"code": r.code, "reason": r.reason[:2000], "retryable": r.retryable,
                "by": dict(auth.actor), "refused": refused, "refused_digest": digest(body),
                "refused_size": len(body), "detail": r.detail}
        if via:
            data["via"] = via
        req2 = {"type": "gateway.rejected", "basis": self.head,
                "idempotency_key": f"gateway:{self.head + 1}", "refs": [], "data": data}
        return self._append(req2, GATEWAY_ACTOR, signature=auth.signature, via=None, verdict=None)
