# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure projections of the fold: inbox, board, entity views (SPEC §16)."""

from __future__ import annotations

from . import vocab
from .policy import Policy

BOARD_ORDER = ("created", "assigned", "in_progress", "blocked", "in_review")


def inbox(state: dict, policy: Policy, for_class: str) -> list[dict]:
    """The inbox for a class. Each item's ``key`` is stable for de-duplication:
    ``kind:entity id:position of the causing event``."""
    items = []
    for kind in policy.inbox.get(for_class, ()):
        for entity, eid, pos, title in vocab.INBOX_RULES[kind](state, for_class):
            items.append({"kind": kind, "entity": entity, "id": eid, "position": pos,
                          "title": title, "key": f"{kind}:{eid}:{pos}"})
    return sorted(items, key=lambda i: (i["position"], i["key"]))


def task_view(state: dict, events: list[dict], task_id: str) -> dict | None:
    task = state["tasks"].get(task_id)
    if task is None:
        return None
    history = [e for e in events if e.get("task") == task_id
               or (e["type"] == "gateway.rejected" and (e["data"].get("refused") or {}).get("task") == task_id)]
    return {"task": task, "events": history}


def goal_view(state: dict, events: list[dict], goal_id: str) -> dict | None:
    goal = state.get("goals", {}).get(goal_id)
    if goal is None:
        return None
    tasks = sorted((t for t in state["tasks"].values() if t["goal"] == goal_id), key=lambda t: t["id"])
    history = [e for e in events if e.get("goal") == goal_id]
    return {"goal": goal, "tasks": tasks, "events": history}


def board(state: dict, *, project: str | None = None) -> str:
    """Open tasks by state, with owner, last activity and review status."""
    tasks = [t for t in state["tasks"].values()
             if t["status"] in BOARD_ORDER and (project is None or t["project"] == project)]
    hive = state["hive"] or {}
    lines = [f"hive {hive.get('hive')}  head {hive.get('head', 0)}  mode {hive.get('mode')}  "
             f"profile {hive.get('profile')}"]
    goals = state.get("goals")
    if goals:
        open_goals = [g for g in goals.values() if g["status"] not in vocab.GOAL_TERMINAL
                      and (project is None or g["project"] == project)]
        if open_goals:
            lines.append("")
            lines.append("GOALS")
            for g in sorted(open_goals, key=lambda g: g["id"]):
                esc = f"  ESCALATED:{g['escalation']['code']}" if g["escalation"] else ""
                lines.append(f"  {g['id']:<24} {g['status']:<10} {g['title']}{esc}")
    for status in BOARD_ORDER:
        group = sorted((t for t in tasks if t["status"] == status), key=lambda t: t["id"])
        if not group:
            continue
        lines.append("")
        lines.append(status.upper())
        for t in group:
            marks = []
            if t["status"] == "in_review":
                lr = t["latest_review"]
                marks.append(f"review:{lr['verdict']}" if lr else
                             f"reviewer:{t['assigned_reviewer']}" if t["assigned_reviewer"] else "review:unassigned")
            if t["block"]:
                marks.append(f"needs:{t['block']['needs']}" + ("(answered)" if t["block"]["answered"] else ""))
            ext = t.get("ext") or {}
            if ext.get("escalation"):
                marks.append(f"ESCALATED:{ext['escalation']['code']}")
            if ext.get("kind") == "review":
                marks.append(f"reviews:{ext['reviews_task']}")
            if t.get("exceptions_applied"):
                marks.append(f"exceptions:{t['exceptions_applied']}")
            if t["violations"]:
                marks.append(f"violations:{t['violations']}")
            owner = t["owner"] or "-"
            lines.append(f"  {t['id']:<24} {owner:<16} @{t['last_activity']['position']:<6} "
                         f"{t['title'][:50]}  {' '.join(marks)}".rstrip())
    return "\n".join(lines) + "\n"
