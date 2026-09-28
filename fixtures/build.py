# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the conformance fixtures (SPEC §21, §27).

    python fixtures/build.py            # rewrite every fixture
    python fixtures/build.py NAME...    # rewrite some (e.g. core/happy-path)

Each scenario runs live against an engine and asserts the outcome of every step,
so a fixture cannot silently record the wrong behavior. The written files are
the golden outputs: tests replay ``requests.jsonl`` and compare.
"""

from __future__ import annotations

import copy
import shutil
import sys
from pathlib import Path

from hiverecord import projections
from hiverecord.canonical import dumps
from hiverecord.conformance import (
    FixtureRepo,
    Runner,
    actor_key,
    advance,
    default_declaration,
    pretty,
    write_jsonl,
)

HERE = Path(__file__).resolve().parent
LEASE = {"lease": {"accept_within_seconds": 600, "checkin_every_seconds": 900}}


class Scenario:
    def __init__(self, fx: FixtureRepo, *, profile: str = "core", mode: str = "authoritative") -> None:
        self.fx = fx
        self.r = Runner(fx)
        self.at = "2026-09-01T12:00:00Z"
        self.n = 0
        self.profile = profile
        self.mode = mode
        self.snapshots: list[int] = []

    @property
    def state(self) -> dict:
        return self.r.engine.state

    def task(self, tid: str) -> dict:
        return self.state["tasks"][tid]

    def result_event(self, tid: str) -> str:
        return self.task(tid)["current_result"]["event_id"]

    def last_id(self) -> str:
        return self.r.engine.events[-1]["event_id"]

    def snapshot(self) -> None:
        self.snapshots.append(self.r.engine.head)

    # --- steps ------------------------------------------------------------
    def init(self, operator: str = "op") -> None:
        self.r.run({"op": "init", "at": self.at, "hive": "fixture", "mode": self.mode,
                    "profile": self.profile, "policy": self.fx.policy_pin(),
                    "registry_digest": self.fx.registry_digest,
                    "operator": {"id": operator, "role": "owner", "keys": [actor_key(operator).public],
                                 "declaration": default_declaration(operator)}})
        self.start()

    def start(self) -> None:
        self.at = advance(self.at)
        self.r.run({"op": "start", "at": self.at,
                    "data": {"version": "fixture", "source_commit": "0" * 40,
                             "policy_digest": self.fx.policy_pin()["content_digest"],
                             "registry_digest": self.fx.registry_digest}})

    def req(self, actor: str, type_: str, *, task: str | None = None, goal: str | None = None,
            data: dict | None = None, refs: list | None = None, rev: int | None = None,
            basis: int | None = None, idem: str | None = None, expect: str = "accepted",
            **extra: object) -> dict:
        self.at = advance(self.at)
        self.n += 1
        request = {"type": type_, "basis": self.r.engine.head if basis is None else basis,
                   "idempotency_key": idem or f"{actor}-{self.n}",
                   "refs": [{"rel": rel, "pin": self.fx.pin(p) if isinstance(p, str) else p}
                            for rel, p in (refs or [])],
                   "data": data or {}}
        if task is not None:
            request["task"] = task
        if goal is not None:
            request["goal"] = goal
        if rev is not None:
            request["expected_revision"] = rev
        request.update(extra.pop("fields", {}))
        step = {"op": "request", "at": self.at, "actor": actor, "request": request, **extra}
        resp = self.r.run(step)
        got = resp.get("code", resp["status"])
        if got != expect:
            raise AssertionError(f"step {len(self.r.steps)} {type_} by {actor}: expected {expect}, got {got}")
        return resp

    def register(self, actor: str, cls: str, *, by: str = "op") -> None:
        self.req(by, "actor.registered", data={"actor_id": actor, "class": cls, "role": cls,
                                                "keys": [actor_key(actor).public],
                                                "declaration": default_declaration(actor)})

    # --- write --------------------------------------------------------------
    def write(self, name: str) -> None:
        d = HERE / name
        if d.exists():
            shutil.rmtree(d)
        (d / "expected").mkdir(parents=True)
        write_jsonl(d / "requests.jsonl", self.r.steps)
        write_jsonl(d / "expected" / "responses.jsonl", self.r.responses)
        (d / "log.jsonl").write_bytes(self.r.log_lines())
        (d / "policy").write_text(
            "The policy tree is the fixture repository's policy/ (fixtures/make-repo.sh), "
            f"pinned as:\n{pretty(self.fx.policy_pin())}")
        positions = sorted({*self.snapshots, self.r.engine.head})
        for pos in positions:
            eng = replay_to(self.fx, self.r.steps, pos)
            (d / "expected" / f"state@{pos}.json").write_text(pretty(eng.state))
            (d / "expected" / f"inbox@{pos}.json").write_text(pretty(
                {cls: projections.inbox(eng.state, eng.policy, cls) for cls in sorted(eng.policy.inbox)}))


def replay_to(fx: FixtureRepo, steps: list[dict], pos: int):
    """The fold at ``pos``, from a fresh standalone fold of the log prefix."""
    from hiverecord.conformance import fold_log
    runner = Runner(fx)
    for s in steps:
        runner.run(copy.deepcopy(s))
    return fold_log([e for e in runner.engine.events if e["position"] <= pos], [fx.policy_dir])


# --------------------------------------------------------------------------- #
# core scenarios
# --------------------------------------------------------------------------- #
def core_setup(s: Scenario) -> None:
    s.init()
    s.register("coord", "coordinator")
    for w in ("w1", "w2"):
        s.register(w, "worker")
    for rv in ("rv1", "rv2"):
        s.register(rv, "reviewer")
    s.register("inst", "instrument")
    s.register("arb", "arbiter")
    s.req("coord", "project.opened", data={"project": "p1", "title": "Project one"})


def to_review(s: Scenario, tid: str, worker: str = "w1", *, order: str = "order-1",
              result: str = "result-1", project: str | None = "p1", goal: str | None = None,
              ext_create: dict | None = None, ext_assign: dict | None = None, by: str = "coord") -> None:
    data = {"title": f"Task {tid}"}
    if project:
        data["project"] = project
    if ext_create:
        data["ext"] = ext_create
    s.req(by, "task.created", task=tid, goal=goal, data=data, refs=[("order", order)])
    adata = {"to": worker}
    if ext_assign:
        adata["ext"] = ext_assign
    s.req(by, "task.assigned", task=tid, data=adata)
    s.req(worker, "task.accepted", task=tid)
    s.req(worker, "task.result_posted", task=tid, refs=[("result", result)])


def review(s: Scenario, tid: str, reviewer: str, verdict: str, *, by: str = "coord",
           review_pin: str = "review-1", expect: str = "accepted") -> None:
    s.req(by, "review.assigned", task=tid, data={"reviewer": reviewer, "result_event": s.result_event(tid)})
    s.req(reviewer, "review.recorded", task=tid, data={"verdict": verdict, "result_event": s.result_event(tid)},
          refs=[("review", review_pin)], expect=expect)


def happy_path(fx: FixtureRepo) -> Scenario:
    s = Scenario(fx)
    core_setup(s)
    to_review(s, "t1")
    s.snapshot()
    review(s, "t1", "rv1", "passed")
    s.snapshot()
    s.req("coord", "task.closed", task="t1", rev=s.task("t1")["revision"], data={"note": "done"})
    return s


def review_loop(fx: FixtureRepo) -> Scenario:
    s = Scenario(fx)
    core_setup(s)
    to_review(s, "t1")
    review(s, "t1", "rv1", "failed")
    s.snapshot()
    s.req("w1", "task.result_posted", task="t1", refs=[("result", "result-2")])
    review(s, "t1", "rv1", "needs_information", review_pin="review-2")
    s.req("w1", "task.reported", task="t1", data={"kind": "finding"}, refs=[("report", "report-1")])
    s.req("w1", "task.result_posted", task="t1", refs=[("result", "result-3")])
    review(s, "t1", "rv2", "passed", review_pin="review-3")
    s.req("coord", "task.closed", task="t1", rev=s.task("t1")["revision"])
    return s


def stale_review(fx: FixtureRepo) -> Scenario:
    s = Scenario(fx)
    core_setup(s)
    to_review(s, "t1")
    first = s.result_event("t1")
    review(s, "t1", "rv1", "failed")
    s.req("w1", "task.result_posted", task="t1", refs=[("result", "result-2")])
    # a review of the superseded result, and an assignment to it
    s.req("coord", "review.assigned", task="t1", data={"reviewer": "rv1", "result_event": first}, expect="REVIEW_STALE")
    s.req("rv1", "review.recorded", task="t1", data={"verdict": "passed", "result_event": first},
          refs=[("review", "review-2")], expect="REVIEW_STALE")
    s.req("coord", "task.closed", task="t1", expect="REVIEW_REQUIRED")
    review(s, "t1", "rv1", "passed", review_pin="review-3")
    s.req("coord", "task.closed", task="t1")
    return s


def block_answer(fx: FixtureRepo) -> Scenario:
    s = Scenario(fx)
    core_setup(s)
    s.req("coord", "task.created", task="t1", data={"title": "Task t1", "project": "p1"}, refs=[("order", "order-1")])
    s.req("coord", "task.assigned", task="t1", data={"to": "w1"})
    s.req("w1", "task.accepted", task="t1")
    s.req("w1", "task.blocked", task="t1", data={"needs": "decision", "reason": "Which API?"},
          refs=[("question", "question-1")])
    s.snapshot()
    s.req("w1", "task.unblocked", task="t1", expect="NOT_ANSWERED")
    s.req("w1", "task.result_posted", task="t1", refs=[("result", "result-1")], expect="ILLEGAL_TRANSITION")
    s.req("coord", "task.answered", task="t1", data={"note": "Use v2"}, refs=[("answer", "answer-1")])
    s.snapshot()
    s.req("w1", "task.unblocked", task="t1")
    s.req("w1", "task.blocked", task="t1", data={"needs": "access", "reason": "Need repo access"},
          refs=[("question", "question-2")])
    s.req("w1", "task.unblocked", task="t1")
    return s


def reassign(fx: FixtureRepo) -> Scenario:
    s = Scenario(fx)
    core_setup(s)
    s.req("coord", "task.created", task="t1", data={"title": "Task t1", "project": "p1"}, refs=[("order", "order-1")])
    s.req("coord", "task.assigned", task="t1", data={"to": "w1"})
    s.req("w1", "task.declined", task="t1", data={"reason": "busy"})
    s.req("coord", "task.assigned", task="t1", data={"to": "w1"})
    s.req("w1", "task.accepted", task="t1")
    s.req("coord", "task.reassigned", task="t1", data={"to": "w1", "reason": "again"}, expect="INVALID_ASSIGNEE")
    s.req("coord", "task.reassigned", task="t1", data={"to": "w2", "reason": "w1 is stuck"})
    s.req("w1", "task.reported", task="t1", data={"kind": "progress"}, refs=[("report", "report-1")], expect="NOT_OWNER")
    s.req("w2", "task.accepted", task="t1")
    s.req("w2", "task.released", task="t1", data={"reason": "wrong skills"}, refs=[("report", "report-2")])
    s.snapshot()
    s.req("coord", "task.assigned", task="t1", data={"to": "rv1"})
    s.req("rv1", "task.accepted", task="t1")
    return s


def policy_change(fx: FixtureRepo) -> Scenario:
    s = Scenario(fx)
    core_setup(s)
    to_review(s, "t1")
    s.req("op", "hive.policy_changed", data={"profile": "1-hive", "reason": "adopt the 1-hive extension"},
          refs=[("policy", fx.policy_pin())])
    s.snapshot()
    s.register("cos", "chief_of_staff")
    # t1 was created under core and finishes under 1-hive
    review(s, "t1", "rv1", "passed", by="cos")
    s.req("cos", "task.closed", task="t1")
    s.req("cos", "task.created", task="t2", data={"title": "Task t2"}, refs=[("order", "order-2")],
          expect="SCHEMA_INVALID")  # a goal is now required
    s.req("cos", "goal.proposed", goal="g1", data={"project": "p1", "title": "Goal one", "objective": "Do it",
          "relevance": "Useless if nobody uses it", "budget": {}}, refs=[("goal", "goal-1")])
    s.req("op", "goal.approved", goal="g1")
    s.req("cos", "task.created", task="t2", goal="g1", data={"title": "Task t2", "project": "p1"},
          refs=[("order", "order-2")])
    return s


def mirror_mode(fx: FixtureRepo) -> Scenario:
    s = Scenario(fx, mode="mirror")
    core_setup(s)
    s.req("coord", "task.created", task="t1", data={"title": "Task t1", "project": "p1"}, refs=[("order", "order-1")])
    s.req("coord", "task.assigned", task="t1", data={"to": "w1"})
    s.req("w2", "task.accepted", task="t1")             # NOT_OWNER, recorded with a verdict
    s.req("w1", "task.result_posted", task="t1", refs=[("result", "result-1")])  # legal: the accept was applied
    s.req("coord", "task.closed", task="t1")           # no review: illegal
    s.req("w1", "task.created", task="t2", data={"title": "Task t2"}, refs=[("order", "order-2")])  # class
    s.snapshot()
    # still refused in mirror mode: schema, existence, pins, identity
    s.req("coord", "task.assigned", task="nope", data={"to": "w1"}, expect="UNKNOWN_TASK")
    s.req("coord", "task.created", task="t1", data={"title": "again"}, refs=[("order", "order-1")], expect="TASK_EXISTS")
    s.req("coord", "task.created", task="t3", data={"title": "Task t3", "bogus": 1}, refs=[("order", "order-1")],
          expect="SCHEMA_INVALID")
    s.req("coord", "task.created", task="t3", data={"title": "Task t3"}, refs=[("order", fx.unpublished_pin())],
          expect="PIN_INVALID")
    s.req("w1", "actor.registered", data={"actor_id": "w9", "class": "worker", "role": "w", "keys": [actor_key("w9").public],
          "declaration": default_declaration("w9")}, expect="NOT_AUTHORIZED")
    s.req("w1", "project.closed", data={"project": "p1", "reason": "x"}, expect="NOT_AUTHORIZED")
    return s


def mode_flip(fx: FixtureRepo) -> Scenario:
    s = Scenario(fx, mode="mirror")
    core_setup(s)
    s.req("coord", "task.created", task="t1", data={"title": "Task t1", "project": "p1"}, refs=[("order", "order-1")])
    s.req("w1", "task.accepted", task="t1")            # illegal, mirrored
    s.req("coord", "hive.mode_changed", data={"mode": "authoritative"}, expect="NOT_AUTHORIZED")
    s.req("op", "hive.mode_changed", data={"mode": "authoritative"})
    s.snapshot()
    s.req("w1", "task.reported", task="t1", data={"kind": "progress"}, refs=[("report", "report-1")], expect="NOT_OWNER")
    s.req("op", "hive.mode_changed", data={"mode": "authoritative"}, expect="ILLEGAL_TRANSITION")
    return s


def core_refusals(fx: FixtureRepo) -> Scenario:
    """One refusal per reachable core code, plus the §22.2 deliberate failures."""
    s = Scenario(fx)
    core_setup(s)
    order = [("order", "order-1")]
    t = {"title": "Task", "project": "p1"}
    # structure
    s.req("coord", "task.created", task="x", data=t, refs=order, fields={"extra": 1}, expect="SCHEMA_INVALID")
    s.req("coord", "task.created", task="x", data={**t, "ext": {}}, refs=order, expect="SCHEMA_INVALID")
    s.req("coord", "task.created", data=t, refs=order, expect="SCHEMA_INVALID")  # no task id
    s.req("coord", "task.exploded", task="x", expect="UNKNOWN_EVENT_TYPE")
    s.req("coord", "message.sent", expect="UNKNOWN_EVENT_TYPE")
    s.req("coord", "task.created", task="x", data=t, refs=order, pad=70000, expect="PAYLOAD_TOO_LARGE")
    s.req("coord", "task.created", task="x", data=t, refs=order, pad=9000, expect="PAYLOAD_TOO_LARGE")
    s.req("coord", "task.created", task="x", data=t, refs=order, basis=10_000, expect="STALE_GENERATION")
    s.req("coord", "task.created", task="x", data=t, refs=order, generation="stale", expect="STALE_GENERATION")
    for body in ('{"not json', '[1,2]', '{"basis":1.5}',
                 '{ "type": "task.created" }'):
        s.at = advance(s.at)
        resp = s.r.run({"op": "request", "at": s.at, "actor": "coord", "body": body})
        assert resp.get("code") == "SCHEMA_INVALID", resp
    # idempotency
    s.req("coord", "task.created", task="t1", data=t, refs=order, idem="k1")
    s.req("coord", "task.created", task="t1", data=t, refs=order, idem="k1", basis=s.r.engine.events[-1]["basis"],
          expect="duplicate")
    s.req("coord", "task.created", task="t9", data=t, refs=order, idem="k1", expect="IDEMPOTENCY_CONFLICT")
    # existence
    s.req("coord", "task.created", task="t1", data=t, refs=order, expect="TASK_EXISTS")
    s.req("coord", "task.assigned", task="ghost", data={"to": "w1"}, expect="UNKNOWN_TASK")
    s.req("coord", "project.opened", data={"project": "p1", "title": "again"}, expect="PROJECT_EXISTS")
    s.req("coord", "task.created", task="x", data={**t, "project": "nope"}, refs=order, expect="UNKNOWN_PROJECT")
    s.req("op", "actor.declared", data={"actor_id": "ghost", "declaration": default_declaration("ghost")},
          expect="UNKNOWN_ACTOR")
    s.req("op", "actor.registered", data={"actor_id": "w1", "class": "worker", "role": "w",
                                        "keys": [actor_key("w1-2").public], "declaration": default_declaration("w1")},
        expect="ACTOR_EXISTS")
    # class
    s.req("w1", "task.created", task="x", data=t, refs=order, expect="NOT_AUTHORIZED")
    s.req("inst", "task.assigned", task="t1", data={"to": "w1"}, expect="NOT_AUTHORIZED")
    s.req("w1", "task.cancelled", task="t1", data={"reason": "x"}, expect="NOT_AUTHORIZED")
    s.req("coord", "task.cancelled", task="t1", data={"reason": "x"}, expect="NOT_AUTHORIZED")
    for cls in ("gateway", "chief_of_staff"):
        s.req("op", "actor.registered", data={"actor_id": f"x-{cls.replace('_', '-')}", "class": cls, "role": "x",
              "keys": [actor_key(f"x-{cls}").public], "declaration": default_declaration("x")}, expect="NOT_AUTHORIZED")
    s.req("coord", "hive.initialized", data={}, expect="SCHEMA_INVALID")
    # assignment, owner and state
    s.req("coord", "task.assigned", task="t1", data={"to": "inst"}, expect="INVALID_ASSIGNEE")
    s.req("coord", "task.assigned", task="t1", data={"to": "ghost"}, expect="INVALID_ASSIGNEE")
    s.req("w1", "task.accepted", task="t1", expect="NOT_OWNER")        # unassigned
    s.req("coord", "task.assigned", task="t1", data={"to": "w1"})
    s.req("w2", "task.accepted", task="t1", expect="NOT_OWNER")
    s.req("w1", "task.accepted", task="t1", refs=[("report", "report-1")], expect="REF_NOT_ALLOWED")
    s.req("w1", "task.accepted", task="t1", rev=1, expect="STALE_REVISION")
    s.req("w1", "task.accepted", task="t1", rev=s.task("t1")["revision"])
    s.req("w2", "task.reported", task="t1", data={"kind": "progress"}, refs=[("report", "report-1")], expect="NOT_OWNER")
    s.req("w1", "task.reported", task="t1", data={"kind": "progress"}, expect="REF_MISSING")
    s.req("w1", "task.reported", task="t1", data={"kind": "progress"},
          refs=[("report", "report-1"), ("report", "report-2")], expect="REF_NOT_ALLOWED")
    s.req("coord", "task.closed", task="t1", expect="ILLEGAL_TRANSITION")  # from in_progress
    s.req("w2", "task.result_posted", task="t1", refs=[("result", "result-1")], expect="NOT_OWNER")
    # pins
    good = fx.pin("result-1")
    tampered = {**good, "content_digest": fx.pin("result-2")["content_digest"]}
    malformed = {k: v for k, v in good.items() if k != "object_oid"}
    unknown_repo = {**good, "repository": "elsewhere"}
    for bad in (tampered, malformed, unknown_repo, fx.unpublished_pin(), {**good, "extra": 1}):
        s.req("w1", "task.result_posted", task="t1", refs=[("result", bad)], expect="PIN_INVALID")
    s.req("w1", "task.result_posted", task="t1", refs=[("result", "result-1")])
    # review
    s.req("coord", "task.closed", task="t1", expect="REVIEW_REQUIRED")
    s.req("w1", "review.assigned", task="t1", data={"reviewer": "rv1", "result_event": s.result_event("t1")},
          expect="NOT_AUTHORIZED")   # the owner assigns its own reviewer
    s.req("coord", "review.assigned", task="t1", data={"reviewer": "w2", "result_event": s.result_event("t1")},
          expect="INVALID_ASSIGNEE")
    s.req("w2", "review.recorded", task="t1", data={"verdict": "passed", "result_event": s.result_event("t1")},
          refs=[("review", "review-1")], expect="NOT_AUTHORIZED")   # a worker reviews
    s.req("rv1", "review.recorded", task="t1", data={"verdict": "passed", "result_event": s.result_event("t1")},
          refs=[("review", "review-1")], expect="REVIEW_NOT_ASSIGNED")
    review(s, "t1", "rv1", "failed")
    s.req("coord", "task.closed", task="t1", expect="ILLEGAL_TRANSITION")   # after a failed review
    # a reviewer-class author
    s.req("coord", "task.created", task="t2", data=t, refs=[("order", "order-2")])
    s.req("coord", "task.assigned", task="t2", data={"to": "rv1"})
    s.req("rv1", "task.accepted", task="t2")
    s.req("rv1", "task.result_posted", task="t2", refs=[("result", "result-2")])
    s.req("coord", "review.assigned", task="t2", data={"reviewer": "rv1", "result_event": s.result_event("t2")},
          expect="REVIEW_NOT_INDEPENDENT")
    s.req("rv1", "review.recorded", task="t2", data={"verdict": "passed", "result_event": s.result_event("t2")},
          refs=[("review", "review-2")], expect="REVIEW_NOT_INDEPENDENT")
    review(s, "t2", "rv2", "needs_information", review_pin="review-2")
    stale = s.result_event("t2")
    s.req("rv1", "task.result_posted", task="t2", refs=[("result", "result-3")])
    s.req("coord", "review.assigned", task="t2", data={"reviewer": "rv2", "result_event": stale}, expect="REVIEW_STALE")
    s.req("coord", "task.closed", task="t2", expect="REVIEW_REQUIRED")
    s.req("op", "review.recorded", task="t2", data={"verdict": "passed", "result_event": s.result_event("t2")},
          refs=[("review", "review-3")])      # an operator needs no assignment
    s.req("coord", "task.closed", task="t2")
    s.req("coord", "task.assigned", task="t2", data={"to": "w1"}, expect="ILLEGAL_TRANSITION")  # out of done
    s.req("op", "task.cancelled", task="t2", data={"reason": "x"}, expect="ILLEGAL_TRANSITION")
    # blocks
    s.req("coord", "task.answered", task="t1", refs=[("answer", "answer-1")], expect="ILLEGAL_TRANSITION")
    s.req("w1", "task.blocked", task="t1", data={"needs": "information", "reason": "?"}, refs=[("question", "question-1")])
    s.req("w1", "task.unblocked", task="t1", expect="NOT_ANSWERED")
    s.req("coord", "task.answered", task="t1", refs=[("answer", "answer-1")])
    s.req("w1", "task.unblocked", task="t1")
    # projects
    s.req("op", "project.closed", data={"project": "p1", "reason": "x"}, expect="PROJECT_HAS_OPEN_WORK")
    s.req("coord", "project.opened", data={"project": "p2", "title": "Two"})
    s.req("op", "project.closed", data={"project": "p2", "reason": "unused"})
    s.req("coord", "task.created", task="x", data={**t, "project": "p2"}, refs=order, expect="PROJECT_NOT_OPEN")
    s.req("op", "project.closed", data={"project": "p2", "reason": "again"}, expect="PROJECT_NOT_OPEN")
    # identity
    s.req("w1", "actor.declared", data={"actor_id": "w2", "declaration": default_declaration("w2")}, expect="NOT_OWNER")
    s.req("w1", "actor.declared", data={"actor_id": "w1", "declaration": {**default_declaration("w1"), "harness": "v2"}})
    s.req("op", "actor.key_added", data={"actor_id": "w2", "key": actor_key("op").public}, expect="KEY_EXISTS")
    s.req("op", "actor.key_revoked", data={"actor_id": "w2", "key": actor_key("w1").public, "reason": "x"},
          expect="UNKNOWN_KEY")
    s.req("op", "actor.retired", data={"actor_id": "op", "reason": "x"}, expect="LAST_OPERATOR")
    s.req("op", "actor.retired", data={"actor_id": "inst", "reason": "unused"})
    s.req("op", "actor.declared", data={"actor_id": "inst", "declaration": default_declaration("inst")},
          expect="ACTOR_RETIRED")
    s.req("op", "actor.key_added", data={"actor_id": "w2", "key": actor_key("w2-new").public})
    s.req("w2", "actor.key_revoked", data={"actor_id": "w2", "key": actor_key("w2").public, "reason": "rotated"})
    s.req("op", "hive.policy_changed", data={"profile": "nope", "reason": "x"}, refs=[("policy", fx.policy_pin())],
          expect="POLICY_INVALID")
    s.req("op", "hive.policy_changed", data={"profile": "core", "reason": "x"}, refs=[("policy", fx.pin("order-1"))],
          expect="POLICY_INVALID")
    # exceptions (SPEC §12.6)
    s.req("coord", "task.assigned", task="t1", data={"to": "w2"}, rev=s.task("t1")["revision"],
          expect="ILLEGAL_TRANSITION")
    refusal = s.last_id()
    s.req("w1", "exception.requested", data={"exception_id": "x1", "refusal": refusal, "reason": "mine?"},
          expect="REFUSAL_MISMATCH")
    s.req("coord", "exception.requested", data={"exception_id": "x1", "refusal": refusal, "reason": "hand over"})
    s.req("coord", "exception.requested", data={"exception_id": "x2", "refusal": refusal, "reason": "again"},
          expect="EXCEPTION_EXISTS")
    s.req("arb", "exception.granted", data={"exception_id": "nope", "reason": "x"}, expect="UNKNOWN_EXCEPTION")
    s.req("w1", "task.reported", task="t1", data={"kind": "progress"}, refs=[("report", "report-2")])
    s.req("arb", "exception.granted", data={"exception_id": "x1", "reason": "ok"}, expect="EXCEPTION_NOT_APPLICABLE")
    s.req("arb", "exception.denied", data={"exception_id": "x1", "reason": "the task moved on"})
    s.req("arb", "exception.denied", data={"exception_id": "x1", "reason": "again"}, expect="EXCEPTION_NOT_PENDING")
    s.req("coord", "task.closed", task="t1", rev=s.task("t1")["revision"], expect="ILLEGAL_TRANSITION")
    s.req("coord", "exception.requested", data={"exception_id": "x3", "refusal": s.last_id(), "reason": "close it"},
          expect="NOT_EXCEPTABLE")
    # unauthenticated: 401, nothing recorded
    s.req("stranger", "task.created", task="x", data=t, refs=order, expect="unauthenticated")
    s.req("inst", "task.reported", task="t1", data={"kind": "progress"}, refs=[("report", "report-1")],
          expect="unauthenticated")                    # retired actor
    s.req("w2", "task.accepted", task="t1", expect="unauthenticated")   # revoked key
    s.req("coord", "task.created", task="x", data=t, refs=order, signed_at="2026-09-01T00:00:00Z",
          expect="unauthenticated")                    # expired
    s.req("coord", "task.created", task="x", data=t, refs=order, sign_hive="other-hive", expect="unauthenticated")
    return s


def exception(fx: FixtureRepo) -> Scenario:
    """A task closed too early is reopened by a granted exception (SPEC §12.6)."""
    s = Scenario(fx)
    core_setup(s)
    to_review(s, "t1")
    review(s, "t1", "rv1", "passed")
    s.req("coord", "task.closed", task="t1", rev=s.task("t1")["revision"])
    # the coordinator finds the tests were skipped: reassigning out of done is illegal
    s.req("coord", "task.reassigned", task="t1", rev=s.task("t1")["revision"], idem="reopen-t1",
          data={"to": "w2", "reason": "closed too early: the integration tests were skipped"},
          expect="ILLEGAL_TRANSITION")
    refusal = s.last_id()
    s.req("w1", "exception.requested", data={"exception_id": "e0", "refusal": refusal, "reason": "x"},
          expect="REFUSAL_MISMATCH")
    s.req("coord", "exception.requested", refs=[("rationale", "rationale-1")],
          data={"exception_id": "e1", "refusal": refusal, "reason": "Reopen t1 for w2; see the rationale"})
    s.snapshot()
    s.req("coord", "exception.requested", data={"exception_id": "e9", "refusal": refusal, "reason": "again"},
          expect="EXCEPTION_EXISTS")
    s.req("inst", "exception.advised", data={"exception_id": "e1", "recommendation": "grant",
                                             "reason": "matches the reopen criterion"})
    s.req("coord", "exception.advised", data={"exception_id": "e1", "recommendation": "grant", "reason": "x"},
          expect="NOT_AUTHORIZED")                                   # the requester can't advise
    for who in ("coord", "w1", "rv1"):
        s.req(who, "exception.granted", data={"exception_id": "e1", "reason": "x"}, expect="NOT_AUTHORIZED")
    s.req("w1", "exception.withdrawn", data={"exception_id": "e1", "reason": "x"}, expect="NOT_OWNER")
    s.req("arb", "exception.granted", data={"exception_id": "e1", "reason": "Closed without the integration tests"})
    s.snapshot()
    s.req("arb", "exception.granted", data={"exception_id": "e1", "reason": "again"}, expect="EXCEPTION_NOT_PENDING")
    # a retry of the original request now returns the excepted event
    s.at = advance(s.at)
    ev = s.r.engine.event_by_id(refusal)
    resp = s.r.run({"op": "request", "at": s.at, "actor": "coord", "body": dumps(ev["data"]["refused"]).decode()})
    assert resp["status"] == "duplicate", resp
    s.req("w2", "task.accepted", task="t1")
    s.req("w2", "task.result_posted", task="t1", refs=[("result", "result-2")])
    s.req("coord", "task.closed", task="t1", expect="REVIEW_REQUIRED")  # the old pass no longer counts
    review(s, "t1", "rv2", "passed", review_pin="review-2")
    s.req("coord", "task.closed", task="t1", rev=s.task("t1")["revision"])
    # a denied request
    s.req("coord", "task.created", task="t2", data={"title": "Task t2", "project": "p1"}, refs=[("order", "order-2")])
    s.req("coord", "task.assigned", task="t2", data={"to": "inst"}, rev=s.task("t2")["revision"],
          expect="INVALID_ASSIGNEE")
    s.req("coord", "exception.requested", data={"exception_id": "e2", "refusal": s.last_id(),
                                                 "reason": "the metrics bot can do it"})
    s.req("inst", "exception.advised", data={"exception_id": "e2", "recommendation": "deny",
                                             "reason": "instruments do not own tasks"})
    s.req("op", "exception.denied", data={"exception_id": "e2", "reason": "Assign a worker"})
    # a stale grant: the task changed after the refusal
    s.req("coord", "task.reassigned", task="t2", rev=s.task("t2")["revision"], data={"to": "w1", "reason": "x"},
          expect="ILLEGAL_TRANSITION")
    s.req("coord", "exception.requested", data={"exception_id": "e3", "refusal": s.last_id(), "reason": "hurry"})
    s.req("coord", "task.assigned", task="t2", data={"to": "w2"})
    s.req("arb", "exception.granted", data={"exception_id": "e3", "reason": "ok"}, expect="EXCEPTION_NOT_APPLICABLE")
    s.req("coord", "exception.withdrawn", data={"exception_id": "e3", "reason": "no longer needed"})
    # never waivable, or not exceptable
    s.req("w2", "task.accepted", task="t2")
    s.req("w2", "task.result_posted", task="t2", refs=[("result", "result-3")])
    s.req("coord", "task.closed", task="t2", rev=s.task("t2")["revision"], expect="REVIEW_REQUIRED")
    s.req("coord", "exception.requested", data={"exception_id": "e4", "refusal": s.last_id(), "reason": "x"},
          expect="NOT_EXCEPTABLE")                                   # task.closed / REVIEW_REQUIRED
    s.req("w1", "task.reported", task="t2", rev=s.task("t2")["revision"], data={"kind": "progress"},
          refs=[("report", "report-1")], expect="NOT_OWNER")
    s.req("w1", "exception.requested", data={"exception_id": "e4", "refusal": s.last_id(), "reason": "x"},
          expect="NOT_EXCEPTABLE")                                   # authority
    s.req("w2", "task.reported", task="t2", rev=s.task("t2")["revision"], data={"kind": "progress"},
          refs=[("report", fx.unpublished_pin())], expect="PIN_INVALID")
    s.req("w2", "exception.requested", data={"exception_id": "e4", "refusal": s.last_id(), "reason": "x"},
          expect="NOT_EXCEPTABLE")                                   # pins
    s.req("w1", "task.created", task="t3", data={"title": "T3"}, refs=[("order", "order-3")], expect="NOT_AUTHORIZED")
    s.req("w1", "exception.requested", data={"exception_id": "e4", "refusal": s.last_id(), "reason": "x"},
          expect="NOT_EXCEPTABLE")                                   # class
    s.req("coord", "task.reassigned", task="t1", data={"to": "w1", "reason": "x"}, expect="ILLEGAL_TRANSITION")
    s.req("coord", "exception.requested", data={"exception_id": "e4", "refusal": s.last_id(), "reason": "x"},
          expect="NOT_EXCEPTABLE")                                   # no expected_revision
    # an operator's own request needs someone else to decide it
    s.req("op", "task.reassigned", task="t1", rev=s.task("t1")["revision"], data={"to": "w1", "reason": "x"},
          expect="ILLEGAL_TRANSITION")
    s.req("op", "exception.requested", data={"exception_id": "e5", "refusal": s.last_id(), "reason": "reopen"})
    s.req("op", "exception.granted", data={"exception_id": "e5", "reason": "x"}, expect="NOT_AUTHORIZED")
    s.snapshot()
    s.req("arb", "exception.denied", data={"exception_id": "e5", "reason": "t1 is fine now"})
    return s


# --------------------------------------------------------------------------- #
# 1-hive scenarios
# --------------------------------------------------------------------------- #
GOAL = {"project": "p1", "title": "Goal one", "objective": "Ship the thing",
        "relevance": "Useless if the thing is not used", "budget": {"usd_micros": 5_000_000, "tokens": 1_000_000}}


def hive1_setup(s: Scenario) -> None:
    s.init()
    s.register("cos", "chief_of_staff")
    s.register("sup", "supervisor")
    s.register("coord", "coordinator")
    for a, c in (("w1", "worker"), ("w2", "worker"), ("rv1", "reviewer"), ("rv2", "reviewer")):
        s.register(a, c)
    s.req("cos", "project.opened", data={"project": "p1", "title": "Project one"})


def active_goal(s: Scenario, gid: str = "g1", **over: object) -> None:
    s.req("cos", "goal.proposed", goal=gid, data={**GOAL, **over}, refs=[("goal", "goal-1")])
    s.req("op", "goal.approved", goal=gid)


def goal_happy_path(fx: FixtureRepo) -> Scenario:
    s = Scenario(fx, profile="1-hive")
    hive1_setup(s)
    s.req("cos", "goal.proposed", goal="g1", data=GOAL, refs=[("goal", "goal-1")])
    s.snapshot()
    s.req("op", "goal.approved", goal="g1")
    s.req("cos", "task.created", task="t1", goal="g1", data={"title": "Build it", "project": "p1"},
          refs=[("order", "order-1")])
    s.req("cos", "task.assigned", task="t1", data={"to": "w1", "ext": LEASE})
    s.req("w1", "task.accepted", task="t1")
    s.req("w1", "task.reported", task="t1", data={"kind": "checkpoint", "ext": {"cost": {"usd_micros": 150_000, "tokens": 12_000}}},
          refs=[("report", "report-1")])
    s.req("w1", "task.result_posted", task="t1", refs=[("result", "result-1")],
          data={"ext": {"cost": {"usd_micros": 50_000, "tokens": 3_000, "wall_clock_seconds": 600}}})
    # an independent review task
    s.req("cos", "task.created", task="rt1", goal="g1",
          data={"title": "Review t1", "project": "p1", "ext": {"kind": "review", "reviews_task": "t1"}},
          refs=[("order", "order-2")])
    s.req("cos", "task.assigned", task="rt1", data={"to": "rv1", "ext": LEASE})
    s.req("cos", "review.assigned", task="t1", data={"reviewer": "rv1", "result_event": s.result_event("t1")})
    s.req("rv1", "task.accepted", task="rt1")
    s.req("rv1", "review.recorded", task="t1", data={"verdict": "passed", "result_event": s.result_event("t1"),
          "ext": {"cost": {"usd_micros": 20_000, "tokens": 2_000}}}, refs=[("review", "review-1")])
    s.req("rv1", "task.result_posted", task="rt1", refs=[("result", "review-1")])
    s.req("op", "review.recorded", task="rt1", data={"verdict": "passed", "result_event": s.result_event("rt1")},
          refs=[("review", "review-2")])
    s.snapshot()
    s.req("cos", "task.closed", task="rt1")
    s.req("cos", "task.closed", task="t1", rev=s.task("t1")["revision"])
    s.snapshot()
    s.req("cos", "goal.completed", goal="g1", data={"outcome": "Shipped"}, refs=[("summary", "summary-1")])
    s.snapshot()
    s.req("op", "goal.accepted", goal="g1", data={"note": "Good"})
    return s


def proposals(fx: FixtureRepo) -> Scenario:
    s = Scenario(fx, profile="1-hive")
    hive1_setup(s)
    s.req("cos", "goal.proposed", goal="g1", data=GOAL, refs=[("goal", "goal-1")])
    approve_g1 = {"type": "goal.approved", "goal": "g1", "basis": s.r.engine.head, "idempotency_key": "want-g1",
                  "refs": [], "data": {}}
    s.req("cos", "proposal.submitted", data={"proposal_id": "pr1", "proposed": approve_g1, "approver_class": "operator"},
          refs=[("rationale", "rationale-1")])
    s.snapshot()
    s.req("coord", "proposal.approved", data={"proposal_id": "pr1"}, expect="NOT_AUTHORIZED")
    s.req("op", "proposal.approved", data={"proposal_id": "pr1"})          # appends two events
    s.req("op", "proposal.approved", data={"proposal_id": "pr1"}, expect="PROPOSAL_NOT_PENDING")
    # a worker proposes a task; a coordinator approves
    new_task = {"type": "task.created", "task": "t9", "goal": "g1", "basis": s.r.engine.head,
                "idempotency_key": "want-t9", "refs": [{"rel": "order", "pin": fx.pin("order-3")}],
                "data": {"title": "Split out the parser", "project": "p1"}}
    s.req("w1", "proposal.submitted", data={"proposal_id": "pr2", "proposed": new_task, "approver_class": "coordinator"})
    s.req("w2", "proposal.withdrawn", data={"proposal_id": "pr2", "reason": "not mine"}, expect="NOT_OWNER")
    s.req("coord", "proposal.approved", data={"proposal_id": "pr2"})
    # rejected, and withdrawn
    s.req("w1", "proposal.submitted", data={"proposal_id": "pr3", "proposed": {**new_task, "task": "t10"},
          "approver_class": "chief_of_staff"})
    s.req("cos", "proposal.rejected", data={"proposal_id": "pr3", "reason": "not now"})
    s.req("w1", "proposal.submitted", data={"proposal_id": "pr4", "proposed": {**new_task, "task": "t11"},
          "approver_class": "coordinator"})
    s.req("w1", "proposal.withdrawn", data={"proposal_id": "pr4", "reason": "changed my mind"})
    # failures
    s.req("w1", "proposal.submitted", data={"proposal_id": "pr1", "proposed": new_task, "approver_class": "operator"},
          expect="PROPOSAL_EXISTS")
    s.req("op", "proposal.approved", data={"proposal_id": "nope"}, expect="UNKNOWN_PROPOSAL")
    nested = {"type": "proposal.approved", "basis": 0, "idempotency_key": "n", "refs": [], "data": {"proposal_id": "pr2"}}
    s.req("w1", "proposal.submitted", data={"proposal_id": "pr5", "proposed": nested, "approver_class": "operator"},
          expect="SCHEMA_INVALID")
    s.req("w1", "proposal.submitted", data={"proposal_id": "pr5", "proposed": {"type": "task.created"},
          "approver_class": "operator"}, expect="SCHEMA_INVALID")
    # approval after the state moved on
    s.req("cos", "goal.proposed", goal="g2", data={**GOAL, "title": "Goal two"}, refs=[("goal", "goal-2")])
    s.req("cos", "proposal.submitted", data={"proposal_id": "pr6", "approver_class": "operator",
          "proposed": {**approve_g1, "goal": "g2", "idempotency_key": "want-g2"}})
    s.req("op", "goal.approved", goal="g2")
    s.req("op", "proposal.approved", data={"proposal_id": "pr6"}, expect="PROPOSAL_NOT_APPLICABLE")
    s.req("cos", "proposal.withdrawn", data={"proposal_id": "pr6", "reason": "already approved"})
    return s


def escalations(fx: FixtureRepo) -> Scenario:
    s = Scenario(fx, profile="1-hive")
    hive1_setup(s)
    active_goal(s)
    s.req("cos", "task.created", task="t1", goal="g1", data={"title": "Build it", "project": "p1"}, refs=[("order", "order-1")])
    s.req("cos", "task.assigned", task="t1", data={"to": "w1", "ext": LEASE})
    s.req("w1", "task.accepted", task="t1")
    s.req("w1", "task.escalated", task="t1", data={"to": "chief_of_staff", "code": "question", "reason": "Unclear order"},
          refs=[("diagnosis", "diagnosis-1")])
    s.req("w2", "task.escalated", task="t1", data={"to": "operator", "code": "stuck", "reason": "x"}, expect="NOT_OWNER")
    s.snapshot()
    s.req("cos", "task.escalation_resolved", task="t1", data={"resolution": "Clarified in the order"})
    s.req("cos", "task.escalation_resolved", task="t1", data={"resolution": "again"}, expect="NOT_ESCALATED")
    s.req("sup", "goal.escalated", goal="g1", data={"to": "operator", "code": "at_risk", "reason": "Two failed attempts"})
    s.snapshot()
    s.req("op", "goal.escalation_resolved", goal="g1", data={"resolution": "Keep going"})
    s.req("sup", "task.escalated", task="t1", data={"to": "operator", "code": "repeated_failure", "reason": "3 attempts"})
    s.req("cos", "task.cancelled", task="t1", data={"reason": "Re-planned"})     # closes the escalation
    s.req("op", "goal.abandoned", goal="g1", data={"reason": "Superseded"})
    return s


def reassign_restart(fx: FixtureRepo) -> Scenario:
    s = Scenario(fx, profile="1-hive")
    hive1_setup(s)
    active_goal(s)
    s.req("cos", "task.created", task="t1", goal="g1", data={"title": "Build it", "project": "p1"}, refs=[("order", "order-1")])
    s.req("cos", "task.assigned", task="t1", data={"to": "w1"}, expect="SCHEMA_INVALID")   # no lease
    s.req("cos", "task.assigned", task="t1", data={"to": "w1", "ext": LEASE})
    s.req("w1", "task.accepted", task="t1")
    s.req("sup", "task.nudged", task="t1", data={"reason": "silent past check-in"})
    s.req("sup", "task.nudged", task="t1", data={"reason": "still silent", "message": "Please checkpoint"},
          refs=[("diagnosis", "diagnosis-1")])
    s.snapshot()
    s.req("w1", "task.reported", task="t1", data={"kind": "checkpoint"}, refs=[("report", "report-1")])
    s.req("sup", "task.nudged", task="t1", data={"reason": "silent again"})
    s.req("sup", "task.restarted", task="t1", data={"reason": "process died"}, expect="REF_MISSING")
    s.req("sup", "task.restarted", task="t1", data={"reason": "process died"},
          refs=[("context", "context-1"), ("diagnosis", "diagnosis-1")])
    s.snapshot()
    s.req("sup", "task.reassigned", task="t1", data={"to": "w2", "reason": "3 attempts", "ext": LEASE})
    s.req("sup", "task.closed", task="t1", expect="NOT_AUTHORIZED")
    s.req("sup", "task.cancelled", task="t1", data={"reason": "x"}, expect="NOT_AUTHORIZED")
    s.req("w2", "task.accepted", task="t1")
    return s


def budget(fx: FixtureRepo) -> Scenario:
    s = Scenario(fx, profile="1-hive")
    hive1_setup(s)
    active_goal(s, budget={"usd_micros": 200_000, "tokens": 50_000})
    s.req("cos", "goal.revised", goal="g1", data={"reason": "tighter", "budget": {"usd_micros": 150_000, "tokens": 50_000}})
    s.req("cos", "goal.revised", goal="g1", data={"reason": "more", "budget": {"usd_micros": 300_000, "tokens": 50_000}},
          expect="BUDGET_RAISE_REQUIRES_OPERATOR")
    s.req("cos", "goal.revised", goal="g1", data={"reason": "unlimited", "budget": {"tokens": 50_000}},
          expect="BUDGET_RAISE_REQUIRES_OPERATOR")
    s.req("cos", "goal.revised", goal="g1", data={"reason": "retitle", "title": "Goal one, narrowed"},
          refs=[("goal", "goal-2")])
    s.req("cos", "task.created", task="t1", goal="g1", data={"title": "Build it", "project": "p1"}, refs=[("order", "order-1")])
    s.req("cos", "task.assigned", task="t1", data={"to": "w1", "ext": LEASE})
    s.req("w1", "task.accepted", task="t1")
    for i in range(1, 3):
        s.req("w1", "task.reported", task="t1", refs=[("report", f"report-{i}")],
              data={"kind": "progress", "ext": {"cost": {"usd_micros": 100_000, "tokens": 20_000, "wall_clock_seconds": 300}}})
    s.req("sup", "goal.escalated", goal="g1", data={"to": "operator", "code": "budget_exceeded",
          "reason": "usd 200000 of 150000"})
    s.snapshot()
    s.req("op", "goal.revised", goal="g1", data={"reason": "worth it", "budget": {"usd_micros": 400_000, "tokens": 100_000}})
    s.req("op", "goal.escalation_resolved", goal="g1", data={"resolution": "Budget raised"})
    return s


def hive1_refusals(fx: FixtureRepo) -> Scenario:
    """One refusal per reachable extension code, plus the §27 deliberate failures."""
    s = Scenario(fx, profile="1-hive")
    hive1_setup(s)
    t = {"title": "Task", "project": "p1"}
    order = [("order", "order-1")]
    s.req("cos", "goal.proposed", goal="g1", data=GOAL, refs=[("goal", "goal-1")])
    s.req("cos", "goal.proposed", goal="g1", data=GOAL, refs=[("goal", "goal-1")], expect="GOAL_EXISTS")
    s.req("cos", "goal.proposed", goal="g2", data={**GOAL, "project": "nope"}, refs=[("goal", "goal-1")],
          expect="UNKNOWN_PROJECT")
    s.req("cos", "task.created", task="t1", goal="g1", data=t, refs=order, expect="GOAL_NOT_ACTIVE")
    s.req("cos", "task.created", task="t1", goal="nope", data=t, refs=order, expect="UNKNOWN_GOAL")
    s.req("cos", "task.created", task="t1", data=t, refs=order, expect="SCHEMA_INVALID")      # no goal
    s.req("cos", "goal.approved", goal="g1", expect="NOT_AUTHORIZED")
    s.req("sup", "goal.approved", goal="nope", expect="NOT_AUTHORIZED")
    s.req("op", "goal.approved", goal="nope", expect="UNKNOWN_GOAL")
    s.req("op", "goal.approved", goal="g1")
    s.req("op", "goal.approved", goal="g1", expect="ILLEGAL_TRANSITION")
    s.req("cos", "goal.revised", goal="g1", data={"reason": "x", "budget": {"usd_micros": 9_000_000, "tokens": 1_000_000}},
          expect="BUDGET_RAISE_REQUIRES_OPERATOR")
    s.req("cos", "task.created", task="t1", goal="g1", data=t, refs=order)
    s.req("cos", "task.created", task="t1", goal="g1", data=t, refs=order, expect="TASK_EXISTS")
    s.req("cos", "task.created", task="rt1", goal="g1", data={**t, "ext": {"kind": "review", "reviews_task": "t1"}},
          refs=order, expect="INVALID_REVIEW_TARGET")      # t1 is not in review
    s.req("cos", "task.created", task="rt1", goal="g1", data={**t, "ext": {"kind": "review"}},
          refs=order, expect="SCHEMA_INVALID")             # no reviews_task
    s.req("cos", "goal.completed", goal="g1", data={"outcome": "x"}, refs=[("summary", "summary-1")],
          expect="GOAL_HAS_OPEN_TASKS")
    s.req("op", "project.closed", data={"project": "p1", "reason": "x"}, expect="PROJECT_HAS_OPEN_WORK")
    s.req("cos", "task.escalation_resolved", task="t1", data={"resolution": "x"}, expect="NOT_ESCALATED")
    s.req("cos", "task.assigned", task="t1", data={"to": "w1", "ext": LEASE})
    s.req("w1", "task.accepted", task="t1")
    s.req("w1", "task.result_posted", task="t1", refs=[("result", "result-1")])
    s.req("cos", "task.created", task="rt1", goal="g1", data={**t, "ext": {"kind": "review", "reviews_task": "t1"}},
          refs=order)
    s.req("coord", "task.assigned", task="rt1", data={"to": "w1", "ext": LEASE}, expect="REVIEW_NOT_INDEPENDENT")
    s.req("sup", "task.closed", task="t1", expect="NOT_AUTHORIZED")
    s.req("op", "proposal.approved", data={"proposal_id": "nope"}, expect="UNKNOWN_PROPOSAL")
    s.req("w1", "proposal.submitted", data={"proposal_id": "pr1", "approver_class": "coordinator",
          "proposed": {"type": "task.closed", "task": "t1", "basis": 0, "idempotency_key": "c", "refs": [], "data": {}}})
    s.req("w1", "proposal.submitted", data={"proposal_id": "pr1", "approver_class": "coordinator",
          "proposed": {"type": "task.closed", "task": "t1", "basis": 0, "idempotency_key": "c", "refs": [], "data": {}}},
          expect="PROPOSAL_EXISTS")
    s.req("coord", "proposal.approved", data={"proposal_id": "pr1"}, expect="PROPOSAL_NOT_APPLICABLE")  # no review
    s.req("w1", "proposal.withdrawn", data={"proposal_id": "pr1", "reason": "premature"})
    s.req("w1", "proposal.withdrawn", data={"proposal_id": "pr1", "reason": "again"}, expect="PROPOSAL_NOT_PENDING")
    s.req("cos", "goal.accepted", goal="g1", expect="NOT_AUTHORIZED")
    # exceptions: the chief of staff can't decide them; the request waits in the operator's inbox
    s.req("cos", "task.answered", task="t1", rev=s.task("t1")["revision"], refs=[("answer", "answer-1")],
          expect="ILLEGAL_TRANSITION")
    s.req("cos", "exception.requested", data={"exception_id": "x1", "refusal": s.last_id(), "reason": "late answer"})
    s.req("cos", "exception.granted", data={"exception_id": "x1", "reason": "x"}, expect="NOT_AUTHORIZED")
    s.req("w1", "exception.denied", data={"exception_id": "x1", "reason": "x"}, expect="NOT_AUTHORIZED")
    return s


SCENARIOS = {
    "core/happy-path": happy_path,
    "core/review-loop": review_loop,
    "core/stale-review": stale_review,
    "core/block-answer": block_answer,
    "core/reassign": reassign,
    "core/policy-change": policy_change,
    "core/mirror-mode": mirror_mode,
    "core/mode-flip": mode_flip,
    "core/exception": exception,
    "core/refusals": core_refusals,
    "1-hive/goal-happy-path": goal_happy_path,
    "1-hive/proposals": proposals,
    "1-hive/escalations": escalations,
    "1-hive/reassign-restart": reassign_restart,
    "1-hive/budget": budget,
    "1-hive/refusals": hive1_refusals,
}


def main(argv: list[str]) -> None:
    fx = FixtureRepo()
    names = argv or list(SCENARIOS)
    for name in names:
        SCENARIOS[name](fx).write(name)
        print(f"wrote fixtures/{name}")


if __name__ == "__main__":
    main(sys.argv[1:])
