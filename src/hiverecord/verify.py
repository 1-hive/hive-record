# SPDX-License-Identifier: GPL-3.0-or-later
"""Re-verify an exported log from its bytes alone (SPEC §6.4, §22.1, §24.4)."""

from __future__ import annotations

from . import keys
from .canonical import dumps, loads, sha256_hex
from .engine import Engine, request_of


def verify_log(lines: list[bytes], engine: Engine) -> list[str]:
    """Fold ``lines`` into a fresh ``engine`` and return every problem found:
    non-canonical lines, bad signatures, keys not held by the event's actor at
    that position, applied events without their approval, and excepted events
    that do not follow their grant."""
    problems: list[str] = []
    prev: dict | None = None
    for raw in lines:
        ev = loads(raw)
        pos = ev.get("position")
        if dumps(ev) != raw:
            problems.append(f"{pos}: not canonical")
        sig = ev.get("signature")
        hive = ev.get("hive")
        if ev["type"] == "gateway.rejected":
            if sig:
                body_hex = ev["data"]["refused_digest"].split(":", 1)[1]
                if not keys.verify(sig["key"], keys.write_string(hive, sig["signed_at"], body_hex), sig["sig"]):
                    problems.append(f"{pos}: refused request's signature does not verify")
        elif ev["actor"]["class"] == "gateway":
            if sig:
                problems.append(f"{pos}: gateway event carries a signature")
        elif "on_proposal" in ev:
            ok = (prev is not None and prev["type"] == "proposal.approved"
                  and prev["data"]["proposal_id"] == ev["on_proposal"] and prev["actor"] == ev["actor"])
            prop = engine.state.get("proposals", {}).get(ev["on_proposal"], {})
            copied = {k: prop.get("proposed", {}).get(k) for k in ("type", "task", "goal", "refs", "data")}
            if not ok or any(copied[k] != ev.get(k) for k in copied if copied[k] is not None or k in ev):
                problems.append(f"{pos}: applied event does not match an approval of its proposal")
        elif not sig:
            problems.append(f"{pos}: unsigned event")
        else:
            body_hex = sha256_hex(dumps(request_of(ev)))
            if not keys.verify(sig["key"], keys.write_string(hive, sig["signed_at"], body_hex), sig["sig"]):
                problems.append(f"{pos}: signature does not verify")
            elif engine.actor_for_key(sig["key"]) != ev["actor"]:
                problems.append(f"{pos}: the key did not belong to {ev['actor']['id']} at this position")
            if "on_exception" in ev and not (
                    prev is not None and prev["type"] == "exception.granted"
                    and prev["data"]["exception_id"] == ev["on_exception"]):
                problems.append(f"{pos}: excepted event does not follow the grant of its exception")
        engine.apply(ev)
        prev = ev
    return problems
