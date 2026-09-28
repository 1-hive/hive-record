# SPDX-License-Identifier: GPL-3.0-or-later
"""Property tests (SPEC §22.3, §27): random well-formed requests from random
actors, in both modes, under every shipped profile."""

from __future__ import annotations

import random

import pytest

from hiverecord.canonical import digest, dumps
from hiverecord.conformance import Runner, actor_key, advance, default_declaration, fold_log
from hiverecord.engine import Engine, request_of
from hiverecord.errors import Refusal
from hiverecord.pins import PolicyResolver

ACTORS = {
    "core": {"op": "operator", "coord": "coordinator", "w1": "worker", "w2": "worker",
             "rv1": "reviewer", "rv2": "reviewer", "inst": "instrument", "arb": "arbiter"},
    "1-hive": {"op": "operator", "cos": "chief_of_staff", "sup": "supervisor", "coord": "coordinator",
               "w1": "worker", "w2": "worker", "rv1": "reviewer", "rv2": "reviewer", "arb": "arbiter"},
}
PIN_NAMES = ["order-1", "result-1", "result-2", "review-1", "question-1", "answer-1", "report-1",
             "goal-1", "summary-1", "context-1", "diagnosis-1", "rationale-1"]
LEASE = {"accept_within_seconds": 60, "checkin_every_seconds": 60}


class Fuzzer:
    def __init__(self, fx, profile: str, mode: str, seed: int) -> None:
        self.fx, self.profile, self.rng = fx, profile, random.Random(seed)
        self.r = Runner(fx)
        self.at = "2026-09-01T12:00:00Z"
        self.n = 0
        self.r.run({"op": "init", "at": self.at, "hive": "prop", "mode": mode, "profile": profile,
                    "policy": fx.policy_pin(), "registry_digest": fx.registry_digest,
                    "operator": {"id": "op", "role": "op", "keys": [actor_key("op").public],
                                 "declaration": default_declaration("op")}})
        for aid, cls in ACTORS[profile].items():
            if aid != "op":
                self.send("op", {"type": "actor.registered", "data": {
                    "actor_id": aid, "class": cls, "role": cls, "keys": [actor_key(aid).public],
                    "declaration": default_declaration(aid)}})
        self.send("op", {"type": "project.opened", "data": {"project": "p1", "title": "P"}})
        if profile == "1-hive":
            self.send("op", {"type": "goal.proposed", "goal": "g1", "refs": [self.ref("goal")], "data": {
                "project": "p1", "title": "G", "objective": "O", "relevance": "R", "budget": {}}})
            self.send("op", {"type": "goal.approved", "goal": "g1", "data": {}})

    @property
    def state(self) -> dict:
        return self.r.engine.state

    def ref(self, rel: str) -> dict:
        return {"rel": rel, "pin": self.fx.pin(self.rng.choice(PIN_NAMES))}

    def send(self, actor: str, partial: dict) -> dict:
        self.at = advance(self.at, 5)
        self.n += 1
        req = {"basis": self.r.engine.head, "idempotency_key": f"k{self.n}", "refs": [], "data": {}, **partial}
        return self.r.run({"op": "request", "at": self.at, "actor": actor, "request": req})

    def pick(self, xs):
        xs = list(xs)
        return self.rng.choice(xs) if xs else None

    def random_request(self) -> tuple[str, dict]:
        rng, st = self.rng, self.state
        policy = self.r.engine.policy
        actor = rng.choice(list(ACTORS[self.profile]))
        types = [t for t in policy.rules if not t.startswith(("gateway.", "hive.initialized"))]
        if rng.random() < 0.12:
            return self.exception_request()
        open_tasks = [k for k, v in st["tasks"].items() if v["status"] not in ("done", "cancelled")]
        tid = self.pick(open_tasks) if open_tasks and rng.random() < 0.85 else f"t{rng.randint(0, 9)}"
        task = st["tasks"].get(tid, {})
        fitting = [t for t in types if policy.rules[t].entity == "task" and (
            (not task and policy.rules[t].frm is None) or
            (task and isinstance(policy.rules[t].frm, tuple) and task["status"] in policy.rules[t].frm))]
        if fitting and rng.random() < 0.8:
            t = rng.choice(fitting)
        else:
            t = rng.choices(types, [8 if x.startswith(("task.", "review.")) else 1 for x in types])[0]
        rule = policy.rules[t]
        cr = task.get("current_result") or {}
        people = list(ACTORS[self.profile])
        data: dict = {}
        schema = policy.data_schemas[t].schema
        props = schema.get("properties", {})
        for f in schema.get("required", []):
            spec = props[f]
            if f == "reviewer" and rng.random() < 0.8:
                data[f] = self.pick(a for a, c in ACTORS[self.profile].items()
                                    if c == "reviewer" and a != cr.get("author")) or "rv1"
            elif f in ("to", "reviewer"):
                data[f] = self.pick([task.get("owner"), *people]) or "w1"
            elif f == "result_event":
                data[f] = cr.get("event_id") or "01a05cd7-2a00-7d82-b082-532b629f6fbe"
            elif f == "project":
                data[f] = "p1"
            elif f == "actor_id":
                data[f] = rng.choice(people)
            elif f in ("key",):
                data[f] = actor_key(rng.choice(people)).public
            elif f == "keys":
                data[f] = [actor_key(f"new{self.n}").public]
            elif f == "declaration":
                data[f] = default_declaration("x")
            elif f == "budget":
                data[f] = {"usd_micros": rng.randint(0, 10**6)}
            elif f == "proposal_id":
                data[f] = self.pick([*st.get("proposals", {}), f"pr{self.n}"])
            elif f == "proposed":
                data[f] = {"type": "task.closed", "task": tid, "basis": 0, "idempotency_key": f"p{self.n}",
                           "refs": [], "data": {}}
            elif "enum" in spec:
                data[f] = rng.choice(spec["enum"])
            elif spec.get("type") == "string":
                data[f] = "x"
        if t == "actor.registered":
            data["class"] = rng.choice(["worker", "reviewer"])
            data["actor_id"] = f"a{self.n}"
        if t in policy.ext_schemas and "lease" in rule.require_ext:
            data["ext"] = {"lease": LEASE}
        elif t in policy.ext_schemas and rng.random() < 0.3 and "cost" in policy.ext_schemas[t].schema["properties"]:
            data["ext"] = {"cost": {"usd_micros": rng.randint(0, 1000), "tokens": rng.randint(0, 1000)}}
        req: dict = {"type": t, "data": data, "refs": [self.ref(r) for r in rule.required]}
        if rule.entity == "task":
            req["task"] = tid if rule.frm is not None or rng.random() < 0.3 else f"t{self.n}"
            if rule.frm is None:
                req["data"].setdefault("title", "T")
                req["data"]["project"] = "p1"
        if rule.entity == "goal":
            req["goal"] = "g1" if rng.random() < 0.7 else f"g{self.n}"
            if rule.frm is None:
                data.update(project="p1", title="G", objective="O", relevance="R", budget={})
        elif rule.require_goal:
            req["goal"] = "g1"
        if "task" in req and task and rng.random() < 0.5:
            req["expected_revision"] = task["revision"]
        # bias actor choice toward someone plausibly allowed
        if rule.relation == "owner" and task.get("owner") and rng.random() < 0.9:
            actor = task["owner"]
        elif t == "review.recorded" and task.get("assigned_reviewer") and rng.random() < 0.7:
            actor = task["assigned_reviewer"]
        elif rule.classes and rng.random() < 0.6:
            allowed = [a for a, c in ACTORS[self.profile].items() if c in rule.classes]
            actor = self.pick(allowed) or actor
        return actor, req

    def exception_request(self) -> tuple[str, dict]:
        """Exception traffic that is often admissible (SPEC §12.6)."""
        rng, st = self.rng, self.state
        pending = [e for e in st["exceptions"].values() if e["status"] == "pending"]
        if pending and rng.random() < 0.6:
            e = rng.choice(pending)
            t = rng.choice(["exception.granted", "exception.granted", "exception.denied", "exception.advised",
                            "exception.withdrawn"])
            actor = {"exception.withdrawn": e["requester"], "exception.advised": "arb"}.get(
                t, rng.choice(["arb", "op", e["requester"]]))
            data = {"exception_id": e["id"], "reason": "x"}
            if t == "exception.advised":
                data["recommendation"] = rng.choice(["grant", "deny"])
            return actor, {"type": t, "data": data}
        refusals = [e for e in self.r.engine.events if e["type"] == "gateway.rejected" and e["data"]["refused"]]
        if not refusals:
            return "op", {"type": "exception.requested", "data": {
                "exception_id": f"x{self.n}", "refusal": "01a05cd7-2a00-7d82-b082-532b629f6fbe", "reason": "x"}}
        waivable = self.r.engine.policy.waivable
        fresh = [e for e in refusals[-3:] if e["data"]["code"] in waivable and "expected_revision" in e["data"]["refused"]]
        ref = rng.choice(fresh) if fresh and rng.random() < 0.8 else rng.choice(refusals[-10:])
        actor = ref["data"]["by"]["id"] if rng.random() < 0.9 else rng.choice(list(ACTORS[self.profile]))
        return actor, {"type": "exception.requested", "data": {
            "exception_id": f"x{self.n}", "refusal": ref["event_id"], "reason": "x"}}

    def run(self, steps: int) -> None:
        for _ in range(steps):
            actor, req = self.random_request()
            self.send(actor, req)


def check_invariants(engine: Engine, fx, mode: str, profile: str) -> None:
    events = engine.events
    # gapless positions, digests match canonical bytes
    assert [e["position"] for e in events] == list(range(1, len(events) + 1))
    for e in events:
        raw = dumps(e)
        assert digest(raw) == digest(dumps(fold_log.__globals__["loads"](raw)))
    # replay: gate-guards hold for every appended event in authoritative mode
    replay = Engine(PolicyResolver(dirs=[fx.policy_dir]))
    created_under: dict[str, str | None] = {}
    for e in events:
        client = e["actor"]["class"] != "gateway" and "on_proposal" not in e
        waive = None
        if "on_exception" in e:
            prev = events[e["position"] - 2]
            assert prev["type"] == "exception.granted" and prev["data"]["exception_id"] == e["on_exception"]
            waive = replay.state["exceptions"][e["on_exception"]]["refusal"]["code"]
            exc = replay.state["exceptions"][e["on_exception"]]
            assert replay.event_by_id(exc["refusal"]["event_id"])["signature"] == e["signature"]
        if client and replay.mode == "authoritative":
            rule = replay.policy.rules[e["type"]]
            try:
                replay._guard(request_of(e), e["actor"], rule, mirror=False, waive=waive)
            except Refusal as r:
                if r.code != "PROPOSAL_NOT_APPLICABLE":
                    raise AssertionError(f"appended event {e['position']} fails its guard: {r.code}") from None
        if e["type"] == "task.created" and profile == "1-hive" and replay.policy.name == "1-hive":
            g = replay.state.get("goals", {}).get(e.get("goal"))
            if e.get("verdict", {"legal": True})["legal"]:
                created_under[e["task"]] = g["status"] if g else None
        replay.apply(e)
    assert replay.state_bytes() == engine.state_bytes()
    # the standalone fold of the export equals the live state
    assert fold_log([dict(e) for e in events], [fx.policy_dir]).state_bytes() == engine.state_bytes()
    # review-gated completion
    for t in engine.state["tasks"].values():
        if t["status"] == "done" and t["violations"] == 0:
            lr, cr = t["latest_review"], t["current_result"]
            assert lr and lr["verdict"] == "passed", t["id"]
            assert lr["reviewer"] != cr["author"], t["id"]
            reviewer = engine.state["actors"][lr["reviewer"]]
            assert reviewer["class"] == "operator" or lr["reviewer"] == t["assigned_reviewer"], t["id"]
    # every task's goal was active at its creation
    for tid, status in created_under.items():
        assert status == "active", tid


CASES = [(p, m, seed) for p in ("core", "1-hive") for m in ("authoritative", "mirror") for seed in range(6)]


@pytest.mark.parametrize("profile,mode,seed", CASES)
def test_random_sequences_keep_invariants(fx, profile, mode, seed):
    f = Fuzzer(fx, profile, mode, seed)
    f.run(150)
    accepted = sum(1 for e in f.r.engine.events if e["actor"]["class"] != "gateway")
    assert accepted > 20, "the generator should produce admissible traffic"
    check_invariants(f.r.engine, fx, mode, profile)


def test_generator_reaches_done(fx):
    """Sanity: across seeds, the random walk closes at least one task."""
    done = 0
    for seed in range(6):
        f = Fuzzer(fx, "core", "authoritative", 100 + seed)
        f.run(500)
        done += sum(t["status"] == "done" for t in f.state["tasks"].values())
    assert done > 0


def test_generator_applies_exceptions(fx):
    """Sanity: across seeds, some grants admit their refused request."""
    applied = 0
    for seed in range(8):
        for mode in ("authoritative", "mirror"):
            f = Fuzzer(fx, "core", mode, 200 + seed)
            f.run(300)
            applied += sum("on_exception" in e for e in f.r.engine.events)
    assert applied > 0
