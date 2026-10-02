# SPDX-License-Identifier: GPL-3.0-or-later
"""The fixed vocabulary of conditions, effects and inbox rules (SPEC §15, §16.2, §25, §26).

Tables and profiles select from these by name. A new entry is a new engine version.
Conditions read state and raise :class:`Refusal`. Effects write state; a ``core``
effect writes core state only and an ``ext`` effect writes extension state only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import Refusal

if TYPE_CHECKING:
    from .policy import Policy, Rule

OPERATOR = "operator"
BUDGET_DIMS = ("usd_micros", "tokens", "wall_clock_seconds")

# Codes the engine itself raises, outside any condition.
ENGINE_CODES = (
    "SCHEMA_INVALID", "UNKNOWN_EVENT_TYPE", "PAYLOAD_TOO_LARGE", "STALE_GENERATION",
    "STALE_REVISION", "IDEMPOTENCY_CONFLICT", "NOT_AUTHORIZED", "NOT_OWNER",
    "ILLEGAL_TRANSITION", "REF_MISSING", "REF_NOT_ALLOWED", "PIN_INVALID", "PIN_UNAVAILABLE",
    "POLICY_INVALID", "INTERNAL_ERROR",
)


@dataclass
class Ctx:
    state: dict
    policy: Policy
    rule: Rule
    req: dict                  # the request (gate) or event (fold)
    actor: dict                # {id, class}
    entity: dict | None
    args: tuple[str, ...] = ()
    event: dict | None = None  # set while folding
    engine: Any = None

    @property
    def data(self) -> dict:
        return self.req.get("data", {})

    def pin(self, rel: str) -> dict | None:
        for r in self.req.get("refs", ()):
            if r["rel"] == rel:
                return r["pin"]
        return None

    def ext(self) -> dict:
        return self.data.get("ext") or {}


def _fail(code: str, reason: str) -> None:
    raise Refusal(code, reason)


TASK_TERMINAL = ("done", "cancelled")
GOAL_TERMINAL = ("accepted", "abandoned")


def _task_open(t: dict) -> bool:
    return t["status"] not in TASK_TERMINAL


def _target(ctx: Ctx) -> str | None:
    return ctx.data.get("to", ctx.data.get("reviewer"))


def _active_actor(state: dict, aid: str | None) -> dict | None:
    a = state["actors"].get(aid) if aid else None
    return a if a and a["status"] == "active" else None


def _author(task: dict | None) -> str | None:
    cr = task and task.get("current_result")
    return cr["author"] if cr else None


def _all_keys(state: dict) -> set[str]:
    out: set[str] = set()
    for a in state["actors"].values():
        out.update(a["keys"])
        out.update(a["revoked_keys"])
    return out


# --------------------------------------------------------------------------- #
# conditions (core)
# --------------------------------------------------------------------------- #
def c_log_empty(ctx: Ctx) -> None:
    if ctx.state["hive"] is not None:
        _fail("ILLEGAL_TRANSITION", "the log is not empty")


def c_class_registrable(ctx: Ctx) -> None:
    cls = ctx.data.get("class")
    if cls not in ctx.policy.classes or cls == "gateway":
        _fail("NOT_AUTHORIZED", f"class {cls!r} cannot be registered under this profile")


def c_keys_unused(ctx: Ctx) -> None:
    used = _all_keys(ctx.state) & set(ctx.data.get("keys", ()))
    if used:
        _fail("KEY_EXISTS", f"key already registered: {sorted(used)[0]}")


def c_key_unused(ctx: Ctx) -> None:
    if ctx.data.get("key") in _all_keys(ctx.state):
        _fail("KEY_EXISTS", "key already registered")


def c_key_is_actors(ctx: Ctx) -> None:
    if not ctx.entity or ctx.data.get("key") not in ctx.entity["keys"]:
        _fail("UNKNOWN_KEY", "not an active key of this actor")


def c_not_last_operator(ctx: Ctx) -> None:
    e = ctx.entity
    if e and e["class"] == OPERATOR:
        others = [a for a in ctx.state["actors"].values()
                  if a["class"] == OPERATOR and a["status"] == "active" and a["id"] != e["id"]]
        if not others:
            _fail("LAST_OPERATOR", "cannot retire the last active operator")


def _project_open(ctx: Ctx, pid: str | None) -> None:
    p = ctx.state["projects"].get(pid)
    if p is None:
        _fail("UNKNOWN_PROJECT", f"unknown project {pid!r}")
    if p["status"] != "open":
        _fail("PROJECT_NOT_OPEN", f"project {pid!r} is not open")


def c_project_open_if_given(ctx: Ctx) -> None:
    if "project" in ctx.data:
        _project_open(ctx, ctx.data["project"])


def c_project_has_no_open_work(ctx: Ctx) -> None:
    pid = ctx.data.get("project")
    if any(t["project"] == pid and _task_open(t) for t in ctx.state["tasks"].values()):
        _fail("PROJECT_HAS_OPEN_WORK", f"project {pid!r} has open tasks")


def c_target_active_class(ctx: Ctx) -> None:
    a = _active_actor(ctx.state, _target(ctx))
    if a is None or a["class"] not in ctx.args:
        _fail("INVALID_ASSIGNEE", f"{_target(ctx)!r} is not an active actor of class {'/'.join(ctx.args)}")


def c_target_is_not_owner(ctx: Ctx) -> None:
    if ctx.entity and _target(ctx) == ctx.entity.get("owner"):
        _fail("INVALID_ASSIGNEE", "the target already owns the task")


def c_answered_if_needed(ctx: Ctx) -> None:
    b = ctx.entity and ctx.entity.get("block")
    if b and b["needs"] not in ("access", "external") and not b["answered"]:
        _fail("NOT_ANSWERED", f"a block needing {b['needs']} must be answered first")


def c_targets_current_result(ctx: Ctx) -> None:
    cr = ctx.entity and ctx.entity.get("current_result")
    if not cr or ctx.data.get("result_event") != cr["event_id"]:
        _fail("REVIEW_STALE", "result_event is not the task's current result")


def c_reviewer_not_author(ctx: Ctx) -> None:
    if ctx.actor["id"] == _author(ctx.entity):
        _fail("REVIEW_NOT_INDEPENDENT", "the author cannot review their own result")


def c_assigned_reviewer_not_author(ctx: Ctx) -> None:
    if ctx.data.get("reviewer") == _author(ctx.entity):
        _fail("REVIEW_NOT_INDEPENDENT", "the author cannot be assigned to review their own result")


def c_reviewer_is_assigned(ctx: Ctx) -> None:
    if ctx.actor["class"] != OPERATOR and (
        not ctx.entity or ctx.entity.get("assigned_reviewer") != ctx.actor["id"]
    ):
        _fail("REVIEW_NOT_ASSIGNED", "not the reviewer assigned to the current result")


def c_current_result_passed(ctx: Ctx) -> None:
    lr = ctx.entity and ctx.entity.get("latest_review")
    if not lr or lr["verdict"] != "passed":
        _fail("REVIEW_REQUIRED", "the latest review of the current result has not passed")


# --------------------------------------------------------------------------- #
# conditions (extension)
# --------------------------------------------------------------------------- #
def _goals(state: dict) -> dict:
    return state.get("goals", {})


def c_project_open(ctx: Ctx) -> None:
    _project_open(ctx, ctx.data.get("project"))


def c_goal_active(ctx: Ctx) -> None:
    g = _goals(ctx.state).get(ctx.req.get("goal"))
    if g is None:
        _fail("UNKNOWN_GOAL", f"unknown goal {ctx.req.get('goal')!r}")
    if g["status"] != "active":
        _fail("GOAL_NOT_ACTIVE", f"goal {g['id']!r} is {g['status']}")


def c_goal_has_no_open_tasks(ctx: Ctx) -> None:
    gid = ctx.req.get("goal")
    if any(t["goal"] == gid and _task_open(t) for t in ctx.state["tasks"].values()):
        _fail("GOAL_HAS_OPEN_TASKS", f"goal {gid!r} has open tasks")


def c_project_has_no_open_goals(ctx: Ctx) -> None:
    pid = ctx.data.get("project")
    if any(g["project"] == pid and g["status"] not in GOAL_TERMINAL for g in _goals(ctx.state).values()):
        _fail("PROJECT_HAS_OPEN_WORK", f"project {pid!r} has open goals")


def c_budget_not_raised_unless_operator(ctx: Ctx) -> None:
    if ctx.actor["class"] == OPERATOR or "budget" not in ctx.data or not ctx.entity:
        return
    old, new = ctx.entity["budget"], ctx.data["budget"]
    for d in BUDGET_DIMS:
        if d in old and (d not in new or new[d] > old[d]):
            _fail("BUDGET_RAISE_REQUIRES_OPERATOR", f"only an operator may raise the {d} budget")


def _escalation(entity: dict | None) -> dict | None:
    if not entity:
        return None
    if "escalation" in entity:
        return entity["escalation"]
    return entity.get("ext", {}).get("escalation")


def c_is_escalated(ctx: Ctx) -> None:
    if not _escalation(ctx.entity):
        _fail("NOT_ESCALATED", "there is no open escalation")


def c_review_task_target_valid(ctx: Ctx) -> None:
    ext = ctx.ext()
    if ext.get("kind") != "review":
        return
    target = ctx.state["tasks"].get(ext.get("reviews_task"))
    if target is None or target["goal"] != ctx.req.get("goal") or target["status"] != "in_review":
        _fail("INVALID_REVIEW_TARGET", "reviews_task must be an in_review task under the same goal")


def c_review_task_assignee_not_author(ctx: Ctx) -> None:
    ext = (ctx.entity or {}).get("ext", {})
    if ext.get("kind") != "review":
        return
    reviewed = ctx.state["tasks"].get(ext.get("reviews_task"))
    if _target(ctx) == _author(reviewed):
        _fail("REVIEW_NOT_INDEPENDENT", "a review task cannot go to the author of the reviewed result")


def c_code_matches_repos(ctx: Ctx) -> None:
    """Amendment A3: a result carries one ``code`` and one ``base`` whole-commit v2
    pin for each repository the order lets the task change (``ext.repos``), and no
    others. Whether each base is an ancestor of its code is checked at admission
    (the profile's ``ancestry``), since git history is not on the log."""
    repos = set((ctx.entity or {}).get("ext", {}).get("repos") or [])
    refs = ctx.req.get("refs", ())
    code = [r["pin"] for r in refs if r["rel"] == "code"]
    base = [r["pin"] for r in refs if r["rel"] == "base"]
    if any(p.get("version") != 2 or "path" in p for p in code + base):
        _fail("CODE_BASE_INVALID", "code and base must be whole-commit v2 pins (no path)")
    code_repos = [p["repository"] for p in code]
    base_repos = [p["repository"] for p in base]
    if len(set(code_repos)) != len(code_repos):
        _fail("CODE_BASE_INVALID", "more than one code ref for one repository")
    missing = sorted(repos - set(code_repos))
    if missing:
        _fail("CODE_MISSING", f"no code ref for {missing[0]}, which the order lets the task change")
    extra = sorted(set(code_repos) - repos)
    if extra:
        _fail("CODE_OUT_OF_SCOPE", f"code ref for {extra[0]}, which the order does not name")
    if sorted(base_repos) != sorted(code_repos):
        _fail("CODE_BASE_INVALID", "each code ref needs exactly one base ref of its repository")


def c_proposed_request_well_formed(ctx: Ctx) -> None:
    code, reason = ctx.engine.check_proposed(ctx.data.get("proposed"))
    if code:
        _fail("SCHEMA_INVALID", f"proposed request: {reason}")


def c_approver_matches(ctx: Ctx) -> None:
    if ctx.entity and ctx.actor["class"] not in (ctx.entity["approver_class"], OPERATOR):
        _fail("NOT_AUTHORIZED", f"this proposal is for a {ctx.entity['approver_class']}")


def c_proposal_applicable(ctx: Ctx) -> None:
    ctx.engine.check_application(ctx)


# --------------------------------------------------------------------------- #
# conditions (exceptions, SPEC §12.6)
# --------------------------------------------------------------------------- #
def _refusal(ctx: Ctx) -> dict | None:
    ev = ctx.engine.event_by_id(ctx.data.get("refusal"))
    return ev if ev and ev["type"] == "gateway.rejected" else None


def c_refusal_is_own(ctx: Ctx) -> None:
    ev = _refusal(ctx)
    if ev is None or ev["data"]["by"]["id"] != ctx.actor["id"] or ev["data"]["refused"] is None:
        _fail("REFUSAL_MISMATCH", "refusal must be one of your own recorded refusals, with its request")


def c_refusal_waivable(ctx: Ctx) -> None:
    ev = _refusal(ctx)
    if ev is None:
        _fail("REFUSAL_MISMATCH", "unknown refusal")
    refused, code = ev["data"]["refused"], ev["data"]["code"]
    rule = ctx.policy.rules.get(refused.get("type"))
    if rule is None or not rule.exceptable:
        _fail("NOT_EXCEPTABLE", f"{refused.get('type')} is not exceptable")
    if code not in ctx.policy.waivable:
        _fail("NOT_EXCEPTABLE", f"{code} can never be waived")
    if rule.entity == "task" and "expected_revision" not in refused:
        _fail("NOT_EXCEPTABLE", "a task request needs expected_revision to be excepted")


def c_refusal_not_excepted(ctx: Ctx) -> None:
    rid = ctx.data.get("refusal")
    for e in ctx.state["exceptions"].values():
        if e["refusal"]["event_id"] == rid and e["status"] in ("pending", "granted"):
            _fail("EXCEPTION_EXISTS", f"exception {e['id']} already covers this refusal")


def c_not_requester(ctx: Ctx) -> None:
    if ctx.entity and ctx.entity["requester"] == ctx.actor["id"]:
        _fail("NOT_AUTHORIZED", "the requester cannot decide or advise on its own exception")


def c_exception_applicable(ctx: Ctx) -> None:
    ctx.engine.check_exception(ctx)


# name -> (function, codes it may raise)
CONDITIONS: dict[str, tuple[Any, tuple[str, ...]]] = {
    "log_empty": (c_log_empty, ("ILLEGAL_TRANSITION",)),
    "class_registrable": (c_class_registrable, ("NOT_AUTHORIZED",)),
    "keys_unused": (c_keys_unused, ("KEY_EXISTS",)),
    "key_unused": (c_key_unused, ("KEY_EXISTS",)),
    "key_is_actors": (c_key_is_actors, ("UNKNOWN_KEY",)),
    "not_last_operator": (c_not_last_operator, ("LAST_OPERATOR",)),
    "project_open_if_given": (c_project_open_if_given, ("UNKNOWN_PROJECT", "PROJECT_NOT_OPEN")),
    "project_has_no_open_work": (c_project_has_no_open_work, ("PROJECT_HAS_OPEN_WORK",)),
    "target_active_class": (c_target_active_class, ("INVALID_ASSIGNEE",)),
    "target_is_not_owner": (c_target_is_not_owner, ("INVALID_ASSIGNEE",)),
    "answered_if_needed": (c_answered_if_needed, ("NOT_ANSWERED",)),
    "targets_current_result": (c_targets_current_result, ("REVIEW_STALE",)),
    "reviewer_not_author": (c_reviewer_not_author, ("REVIEW_NOT_INDEPENDENT",)),
    "assigned_reviewer_not_author": (c_assigned_reviewer_not_author, ("REVIEW_NOT_INDEPENDENT",)),
    "reviewer_is_assigned": (c_reviewer_is_assigned, ("REVIEW_NOT_ASSIGNED",)),
    "current_result_passed": (c_current_result_passed, ("REVIEW_REQUIRED",)),
    # extension
    "project_open": (c_project_open, ("UNKNOWN_PROJECT", "PROJECT_NOT_OPEN")),
    "goal_active": (c_goal_active, ("UNKNOWN_GOAL", "GOAL_NOT_ACTIVE")),
    "goal_has_no_open_tasks": (c_goal_has_no_open_tasks, ("GOAL_HAS_OPEN_TASKS",)),
    "project_has_no_open_goals": (c_project_has_no_open_goals, ("PROJECT_HAS_OPEN_WORK",)),
    "budget_not_raised_unless_operator": (c_budget_not_raised_unless_operator, ("BUDGET_RAISE_REQUIRES_OPERATOR",)),
    "is_escalated": (c_is_escalated, ("NOT_ESCALATED",)),
    "review_task_target_valid": (c_review_task_target_valid, ("INVALID_REVIEW_TARGET",)),
    "review_task_assignee_not_author": (c_review_task_assignee_not_author, ("REVIEW_NOT_INDEPENDENT",)),
    "code_matches_repos": (c_code_matches_repos, ("CODE_MISSING", "CODE_OUT_OF_SCOPE", "CODE_BASE_INVALID")),
    "proposed_request_well_formed": (c_proposed_request_well_formed, ("SCHEMA_INVALID",)),
    "approver_matches": (c_approver_matches, ("NOT_AUTHORIZED",)),
    "proposal_applicable": (c_proposal_applicable, ("PROPOSAL_NOT_APPLICABLE",)),
    "refusal_is_own": (c_refusal_is_own, ("REFUSAL_MISMATCH",)),
    "refusal_waivable": (c_refusal_waivable, ("REFUSAL_MISMATCH", "NOT_EXCEPTABLE")),
    "refusal_not_excepted": (c_refusal_not_excepted, ("EXCEPTION_EXISTS",)),
    "not_requester": (c_not_requester, ("NOT_AUTHORIZED",)),
    "exception_applicable": (c_exception_applicable, ("EXCEPTION_NOT_APPLICABLE",)),
}


# --------------------------------------------------------------------------- #
# effects (core)
# --------------------------------------------------------------------------- #
def _at(ctx: Ctx) -> str:
    return ctx.event["recorded_at"]


def _pos(ctx: Ctx) -> int:
    return ctx.event["position"]


def _new_actor(ctx: Ctx, aid: str, cls: str, role: str, keys: list, decl: dict) -> dict:
    return {
        "id": aid, "class": cls, "role": role, "declaration": decl,
        "role_prompt": ctx.pin("role_prompt"), "status": "active",
        "keys": sorted(keys), "revoked_keys": [],
        "registered_at": _at(ctx), "status_since": _pos(ctx),
    }


def e_init_hive(ctx: Ctx) -> None:
    d = ctx.data
    ctx.state["hive"].update({
        "hive": ctx.event["hive"], "profile": d["profile"], "policy": ctx.pin("policy"),
        "registry_digest": d["registry_digest"], "gateway": None,
    })
    op = d["operator"]
    ctx.state["actors"][op["id"]] = _new_actor(ctx, op["id"], OPERATOR, op["role"], op["keys"], op["declaration"])


def e_set_policy(ctx: Ctx) -> None:
    ctx.state["hive"]["profile"] = ctx.data["profile"]
    ctx.state["hive"]["policy"] = ctx.pin("policy")
    if ctx.engine is not None:
        ctx.engine.switch_policy(ctx.pin("policy"), ctx.data["profile"])


def e_set_registry(ctx: Ctx) -> None:
    ctx.state["hive"]["registry_digest"] = ctx.data["registry_digest"]


def e_record_gateway(ctx: Ctx) -> None:
    ctx.state["hive"]["gateway"] = {
        "version": ctx.data["version"], "source_commit": ctx.data["source_commit"],
        "registry_digest": ctx.data["registry_digest"], "since": _at(ctx), "position": _pos(ctx),
    }


def e_open_project(ctx: Ctx) -> None:
    ctx.entity.update({"title": ctx.data["title"], "opened_at": _at(ctx)})


def e_register_actor(ctx: Ctx) -> None:
    d = ctx.data
    ctx.entity.update(_new_actor(ctx, d["actor_id"], d["class"], d["role"], d["keys"], d["declaration"]))


def e_update_declaration(ctx: Ctx) -> None:
    ctx.entity["declaration"] = ctx.data["declaration"]
    ctx.entity["role_prompt"] = ctx.pin("role_prompt")


def e_add_key(ctx: Ctx) -> None:
    ctx.entity["keys"] = sorted({*ctx.entity["keys"], ctx.data["key"]})


def e_revoke_key(ctx: Ctx) -> None:
    k = ctx.data["key"]
    ctx.entity["keys"] = sorted(set(ctx.entity["keys"]) - {k})
    ctx.entity["revoked_keys"] = sorted({*ctx.entity["revoked_keys"], k})


def e_create_task(ctx: Ctx) -> None:
    ctx.entity.update({
        "project": ctx.data.get("project"), "goal": ctx.req.get("goal"),
        "title": ctx.data["title"], "order": ctx.pin("order"),
        "owner": None, "assigned_at": None,
        "last_activity": {"position": _pos(ctx), "at": _at(ctx)},
        "block": None, "current_result": None, "assigned_reviewer": None, "latest_review": None,
        "results_count": 0, "reviews_failed_count": 0, "reassignments": 0, "exceptions_applied": 0,
        "created_by": ctx.actor["id"], "created_at": _at(ctx), "ext": {},
    })


def e_set_owner(ctx: Ctx) -> None:
    ctx.entity["owner"] = ctx.data["to"]
    ctx.entity["assigned_at"] = _at(ctx)


def e_clear_owner(ctx: Ctx) -> None:
    ctx.entity["owner"] = None
    ctx.entity["assigned_at"] = None


def e_record_block(ctx: Ctx) -> None:
    ctx.entity["block"] = {
        "needs": ctx.data["needs"], "reason": ctx.data["reason"], "question": ctx.pin("question"),
        "answered": False, "answer": None, "position": _pos(ctx),
    }


def e_record_answer(ctx: Ctx) -> None:
    if ctx.entity.get("block"):
        ctx.entity["block"]["answered"] = True
        ctx.entity["block"]["answer"] = ctx.pin("answer")


def e_clear_block(ctx: Ctx) -> None:
    ctx.entity["block"] = None


def e_set_current_result(ctx: Ctx) -> None:
    ctx.entity["current_result"] = {
        "event_id": ctx.event["event_id"], "pin": ctx.pin("result"), "author": ctx.actor["id"],
        "at": _at(ctx), "position": _pos(ctx),
    }
    ctx.entity["latest_review"] = None
    ctx.entity["assigned_reviewer"] = None
    ctx.entity["results_count"] += 1


def e_assign_reviewer(ctx: Ctx) -> None:
    ctx.entity["assigned_reviewer"] = ctx.data["reviewer"]


def e_record_review(ctx: Ctx) -> None:
    v = ctx.data["verdict"]
    ctx.entity["latest_review"] = {
        "event_id": ctx.event["event_id"], "verdict": v, "reviewer": ctx.actor["id"],
        "review_pin": ctx.pin("review"), "position": _pos(ctx),
    }
    if v != "passed":
        ctx.entity["reviews_failed_count"] += 1


def e_count_reassignment(ctx: Ctx) -> None:
    ctx.entity["reassignments"] += 1


def e_create_exception(ctx: Ctx) -> None:
    ev = ctx.engine.event_by_id(ctx.data["refusal"])
    refused = ev["data"]["refused"]
    ctx.entity.update({
        "requester": ctx.actor["id"],
        "refusal": {"event_id": ev["event_id"], "position": ev["position"], "code": ev["data"]["code"],
                    "type": refused.get("type"), "task": refused.get("task")},
        "reason": ctx.data["reason"], "advice": [],
        "decided_by": None, "decided_at": None, "applied_event": None,
    })


def e_record_advice(ctx: Ctx) -> None:
    ctx.entity["advice"].append({"actor": ctx.actor["id"], "recommendation": ctx.data["recommendation"],
                                 "position": _pos(ctx)})


def e_decide_exception(ctx: Ctx) -> None:
    ctx.entity["decided_by"] = ctx.actor["id"]
    ctx.entity["decided_at"] = _at(ctx)


# --------------------------------------------------------------------------- #
# effects (extension state only)
# --------------------------------------------------------------------------- #
def _task_ext(task: dict) -> dict:
    ext = task.setdefault("ext", {})
    for k, v in (("kind", "work"), ("reviews_task", None), ("lease", None), ("attempt", 0),
                 ("nudges_since_activity", 0), ("restarts", 0), ("escalation", None)):
        ext.setdefault(k, v)
    ext.setdefault("spent", {d: 0 for d in BUDGET_DIMS})
    return ext


def e_init_task_ext(ctx: Ctx) -> None:
    ext = _task_ext(ctx.entity)
    ext["kind"] = ctx.ext().get("kind", "work")
    ext["reviews_task"] = ctx.ext().get("reviews_task")
    if "repos" in ctx.ext():    # A3; absent keeps earlier fold states unchanged
        ext["repos"] = list(ctx.ext()["repos"])


def e_set_lease(ctx: Ctx) -> None:
    ext = _task_ext(ctx.entity)
    ext["lease"] = ctx.ext().get("lease")
    ext["attempt"] = 1
    ext["nudges_since_activity"] = 0


def e_increment_attempt(ctx: Ctx) -> None:
    ext = _task_ext(ctx.entity)
    ext["attempt"] += 1
    ext["restarts"] += 1
    ext["nudges_since_activity"] = 0


def e_count_nudge(ctx: Ctx) -> None:
    _task_ext(ctx.entity)["nudges_since_activity"] += 1


def e_reset_nudges(ctx: Ctx) -> None:
    if "ext" in ctx.entity and "nudges_since_activity" in ctx.entity["ext"]:
        ctx.entity["ext"]["nudges_since_activity"] = 0


def e_add_cost(ctx: Ctx) -> None:
    cost = ctx.ext().get("cost")
    if not cost:
        return
    spent = _task_ext(ctx.entity)["spent"]
    for d in BUDGET_DIMS:
        spent[d] += cost.get(d, 0)
    goal = _goals(ctx.state).get(ctx.entity.get("goal"))
    if goal is not None:
        for d in ("usd_micros", "tokens"):
            goal["spent"][d] += cost.get(d, 0)


def e_open_escalation(ctx: Ctx) -> None:
    esc = {"to": ctx.data["to"], "code": ctx.data["code"], "reason": ctx.data["reason"],
           "diagnosis": ctx.pin("diagnosis"), "by": ctx.actor["id"], "position": _pos(ctx)}
    if "escalation" in ctx.entity:
        ctx.entity["escalation"] = esc
    else:
        _task_ext(ctx.entity)["escalation"] = esc


def e_close_escalation(ctx: Ctx) -> None:
    if "escalation" in ctx.entity:
        ctx.entity["escalation"] = None
    elif "escalation" in ctx.entity.get("ext", {}):
        ctx.entity["ext"]["escalation"] = None


def e_create_goal(ctx: Ctx) -> None:
    d = ctx.data
    ctx.entity.update({
        "project": d["project"], "title": d["title"], "objective": d["objective"],
        "relevance": d["relevance"], "budget": d["budget"], "goal_pin": ctx.pin("goal"),
        "proposed_by": ctx.actor["id"], "proposed_at": _at(ctx), "approved_at": None,
        "outcome": None, "summary": None,
        "spent": {"usd_micros": 0, "tokens": 0}, "escalation": None,
    })


def e_mark_goal_approved(ctx: Ctx) -> None:
    ctx.entity["approved_at"] = _at(ctx)


def e_update_goal_fields(ctx: Ctx) -> None:
    for k in ("title", "objective", "relevance", "budget"):
        if k in ctx.data:
            ctx.entity[k] = ctx.data[k]
    if ctx.pin("goal"):
        ctx.entity["goal_pin"] = ctx.pin("goal")


def e_record_goal_outcome(ctx: Ctx) -> None:
    ctx.entity["outcome"] = ctx.data["outcome"]
    ctx.entity["summary"] = ctx.pin("summary")


def e_create_proposal(ctx: Ctx) -> None:
    ctx.entity.update({
        "proposer": ctx.actor["id"], "approver_class": ctx.data["approver_class"],
        "proposed": ctx.data["proposed"], "rationale": ctx.pin("rationale"),
        "submitted_at": _at(ctx), "decided_by": None, "decided_at": None, "reason": None,
    })


def e_decide_proposal(ctx: Ctx) -> None:
    ctx.entity.update({"decided_by": ctx.actor["id"], "decided_at": _at(ctx),
                       "reason": ctx.data.get("reason")})


EFFECTS: dict[str, tuple[str, Any]] = {
    "init_hive": ("core", e_init_hive),
    "set_policy": ("core", e_set_policy),
    "set_registry": ("core", e_set_registry),
    "record_gateway": ("core", e_record_gateway),
    "open_project": ("core", e_open_project),
    "register_actor": ("core", e_register_actor),
    "update_declaration": ("core", e_update_declaration),
    "add_key": ("core", e_add_key),
    "revoke_key": ("core", e_revoke_key),
    "create_task": ("core", e_create_task),
    "set_owner": ("core", e_set_owner),
    "clear_owner": ("core", e_clear_owner),
    "record_block": ("core", e_record_block),
    "record_answer": ("core", e_record_answer),
    "clear_block": ("core", e_clear_block),
    "set_current_result": ("core", e_set_current_result),
    "assign_reviewer": ("core", e_assign_reviewer),
    "record_review": ("core", e_record_review),
    "count_reassignment": ("core", e_count_reassignment),
    "create_exception": ("core", e_create_exception),
    "record_advice": ("core", e_record_advice),
    "decide_exception": ("core", e_decide_exception),
    "init_task_ext": ("ext", e_init_task_ext),
    "set_lease": ("ext", e_set_lease),
    "increment_attempt": ("ext", e_increment_attempt),
    "count_nudge": ("ext", e_count_nudge),
    "reset_nudges": ("ext", e_reset_nudges),
    "add_cost": ("ext", e_add_cost),
    "open_escalation": ("ext", e_open_escalation),
    "close_escalation": ("ext", e_close_escalation),
    "create_goal": ("ext", e_create_goal),
    "mark_goal_approved": ("ext", e_mark_goal_approved),
    "update_goal_fields": ("ext", e_update_goal_fields),
    "record_goal_outcome": ("ext", e_record_goal_outcome),
    "create_proposal": ("ext", e_create_proposal),
    "decide_proposal": ("ext", e_decide_proposal),
}


# --------------------------------------------------------------------------- #
# inbox rules (SPEC §16.2, §26). Each yields (kind, entity kind, id, position, title).
# --------------------------------------------------------------------------- #
def _tasks(state: dict):
    return sorted(state["tasks"].values(), key=lambda t: t["id"])


def i_task_blocked_unanswered(state: dict, _for: str):
    for t in _tasks(state):
        b = t["block"]
        if t["status"] == "blocked" and b and not b["answered"] and b["needs"] in ("decision", "information"):
            yield ("task", t["id"], b["position"], t["title"])


def i_task_blocked_unanswered_any(state: dict, _for: str):
    for t in _tasks(state):
        b = t["block"]
        if t["status"] == "blocked" and b and not b["answered"]:
            yield ("task", t["id"], b["position"], t["title"])


def i_task_passed_not_closed(state: dict, _for: str):
    for t in _tasks(state):
        lr = t["latest_review"]
        if t["status"] == "in_review" and lr and lr["verdict"] == "passed":
            yield ("task", t["id"], lr["position"], t["title"])


def i_task_review_unassigned(state: dict, _for: str):
    for t in _tasks(state):
        if t["status"] == "in_review" and t["current_result"] and not t["assigned_reviewer"]:
            yield ("task", t["id"], t["current_result"]["position"], t["title"])


def _goal_list(state: dict):
    return sorted(_goals(state).values(), key=lambda g: g["id"])


def i_goal_proposed(state: dict, _for: str):
    for g in _goal_list(state):
        if g["status"] == "proposed":
            yield ("goal", g["id"], g["status_since"], g["title"])


def i_goal_completed(state: dict, _for: str):
    for g in _goal_list(state):
        if g["status"] == "completed":
            yield ("goal", g["id"], g["status_since"], g["title"])


def i_proposal_pending(state: dict, for_class: str):
    for p in sorted(state.get("proposals", {}).values(), key=lambda p: p["id"]):
        if p["status"] == "pending" and p["approver_class"] == for_class:
            yield ("proposal", p["id"], p["status_since"], p["proposed"].get("type", ""))


def i_escalation_open(state: dict, for_class: str):
    for g in _goal_list(state):
        e = g["escalation"]
        if e and e["to"] == for_class:
            yield ("goal", g["id"], e["position"], f"{e['code']}: {g['title']}")
    for t in _tasks(state):
        e = t.get("ext", {}).get("escalation")
        if e and e["to"] == for_class:
            yield ("task", t["id"], e["position"], f"{e['code']}: {t['title']}")


def i_goal_abandoned_open_tasks(state: dict, _for: str):
    for g in _goal_list(state):
        if g["status"] == "abandoned" and any(
            t["goal"] == g["id"] and _task_open(t) for t in state["tasks"].values()
        ):
            yield ("goal", g["id"], g["status_since"], g["title"])


def i_goal_active_all_terminal(state: dict, _for: str):
    for g in _goal_list(state):
        if g["status"] != "active":
            continue
        tasks = [t for t in state["tasks"].values() if t["goal"] == g["id"]]
        if all(not _task_open(t) for t in tasks):
            pos = max([g["status_since"], *(t["status_since"] for t in tasks)])
            yield ("goal", g["id"], pos, g["title"])


def i_exception_pending(state: dict, _for: str):
    for e in sorted(state["exceptions"].values(), key=lambda e: e["id"]):
        if e["status"] == "pending":
            r = e["refusal"]
            yield ("exception", e["id"], e["status_since"],
                   f"{r['code']} on {r['type']}" + (f" {r['task']}" if r["task"] else ""))


INBOX_RULES = {
    "exception_pending": i_exception_pending,
    "task_blocked_unanswered": i_task_blocked_unanswered,
    "task_blocked_unanswered_any": i_task_blocked_unanswered_any,
    "task_passed_not_closed": i_task_passed_not_closed,
    "task_review_unassigned": i_task_review_unassigned,
    "goal_proposed": i_goal_proposed,
    "goal_completed": i_goal_completed,
    "proposal_pending": i_proposal_pending,
    "escalation_open": i_escalation_open,
    "goal_abandoned_open_tasks": i_goal_abandoned_open_tasks,
    "goal_active_all_terminal": i_goal_active_all_terminal,
}
