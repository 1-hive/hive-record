# SPDX-License-Identifier: GPL-3.0-or-later
"""Policy loading, composition and the add-or-tighten rule (SPEC §10, §13)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hiverecord.policy import PolicyError, load_policy, schema_errors, tree_digest

POLICY = Path(__file__).parents[1] / "policy"


def test_core_and_1hive_load():
    core = load_policy(POLICY, "core")
    one = load_policy(POLICY, "1-hive")
    assert set(core.rules) < set(one.rules)
    assert "chief_of_staff" in one.rules["task.closed"].classes
    assert "chief_of_staff" not in core.classes


def test_one_rule_and_one_schema_per_type():
    for profile in ("core", "1-hive"):
        p = load_policy(POLICY, profile)
        assert set(p.rules) == set(p.data_schemas)


def test_every_schema_string_has_a_max_length():
    """SPEC §7.4: strings are bounded by their schemas."""
    def walk(node, where):
        if isinstance(node, dict):
            if node.get("type") == "string" and "maxLength" not in node and "enum" not in node:
                raise AssertionError(f"{where}: unbounded string")
            for k, v in node.items():
                walk(v, f"{where}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{where}/{i}")
    for p in POLICY.rglob("*.schema.json"):
        walk(json.loads(p.read_text()), p.name)


@pytest.fixture
def tree(tmp_path):
    dst = tmp_path / "policy"
    shutil.copytree(POLICY, dst)
    return dst


def edit_profile(tree: Path, fn) -> None:
    p = tree / "profiles" / "1-hive" / "profile.json"
    prof = json.loads(p.read_text())
    fn(prof)
    p.write_text(json.dumps(prof))


BAD_EXTENSIONS = {
    "grants a core class": lambda p: p["amend"]["task.closed"].update(add_classes=["worker"]),
    "rule for a core type": lambda p: p["rules"].append({**p["rules"][0], "event": "task.closed"}),
    "moves a core entity": lambda p: p["rules"].append({**p["rules"][-1], "event": "task.forced", "to": "done"}),
    "creates a core entity": lambda p: p["rules"].append({**p["rules"][-1], "event": "task.spawned", "from": None,
                                                          "to": "created"}),
    "core effect in a new rule": lambda p: p["rules"].append({**p["rules"][-1], "event": "task.grab",
                                                             "effects": ["set_owner"]}),
    "core effect in an amendment": lambda p: p["amend"]["task.closed"].update(add_effects=["clear_owner"]),
    "reserved prefix": lambda p: p["rules"].append({**p["rules"][-1], "event": "message.sent"}),
    "unknown condition": lambda p: p["amend"]["task.closed"].update(add_conditions=["always_true"]),
    "unknown effect": lambda p: p["amend"]["task.closed"].update(add_effects=["teleport"]),
    "re-adds a core class": lambda p: p["add_classes"].append("worker"),
    "amends an extension type": lambda p: p["amend"].update({"goal.approved": {"add_classes": ["supervisor"]}}),
    "unknown field": lambda p: p.update(loosen=True),
    "hook with a core effect": lambda p: p["hooks"].update(on_touch=["clear_owner"]),
    "makes a core code waivable": lambda p: p.update(waivable_codes=["NOT_AUTHORIZED"]),
    "makes a core rule exceptable": lambda p: p["amend"]["task.closed"].update(exceptable=True),
    "exceptable identity rule": lambda p: p["rules"].append({**p["rules"][0], "event": "actor.promoted",
                                                             "entity": "actor", "exceptable": True}),
    "makes a core rel repeatable": lambda p: p["repeatable_rels"].append("result"),
    "ancestry over core rels": lambda p: p["ancestry"].append({"ancestor": "order", "descendant": "result",
                                                               "code": "CODE_BASE_INVALID"}),
    "ancestry with a core code": lambda p: p["ancestry"].append({"ancestor": "base", "descendant": "code",
                                                                 "code": "NOT_AUTHORIZED"}),
}


@pytest.mark.parametrize("name", sorted(BAD_EXTENSIONS))
def test_extensions_that_loosen_are_refused(tree, name):
    edit_profile(tree, BAD_EXTENSIONS[name])
    with pytest.raises(PolicyError):
        load_policy(tree, "1-hive")


def test_new_rule_without_schema_is_refused(tree):
    edit_profile(tree, lambda p: p["rules"].append({**p["rules"][-1], "event": "task.poked"}))
    with pytest.raises(PolicyError, match="no data schema"):
        load_policy(tree, "1-hive")


def test_core_schema_cannot_be_redefined(tree):
    shutil.copy(tree / "core/schemas/events/task.closed.schema.json",
                tree / "profiles/1-hive/schemas/events/task.closed.schema.json")
    with pytest.raises(PolicyError):
        load_policy(tree, "1-hive")


def test_unknown_profile(tree):
    with pytest.raises(PolicyError):
        load_policy(tree, "nope")


def test_tree_digest_tracks_content(tree):
    d = tree_digest(tree)
    assert d == tree_digest(POLICY)
    (tree / "core" / "legality.json").write_text((tree / "core" / "legality.json").read_text() + " ")
    assert tree_digest(tree) != d


def test_meta_schema_rejects_bad_table():
    from hiverecord.policy import meta_validator
    table = json.loads((POLICY / "core" / "legality.json").read_text())
    table["rules"][0]["relation"] = "anyone"
    assert schema_errors(meta_validator("legality-table-v1.schema.json"), table)


def test_forbid_exceptions_tightens(tree):
    def fn(p):
        p["amend"]["task.reassigned"]["forbid_exceptions"] = True
        p["waivable_codes"] = ["GOAL_NOT_ACTIVE"]
    edit_profile(tree, fn)
    p = load_policy(tree, "1-hive")
    assert not p.rules["task.reassigned"].exceptable and p.rules["task.assigned"].exceptable
    assert "GOAL_NOT_ACTIVE" in p.waivable


def test_review_gate_is_never_exceptable():
    for profile in ("core", "1-hive"):
        p = load_policy(POLICY, profile)
        assert not any(p.rules[t].exceptable for t in ("review.assigned", "review.recorded", "task.closed"))
        assert not {"NOT_AUTHORIZED", "NOT_OWNER", "REVIEW_REQUIRED", "PIN_INVALID", "STALE_REVISION"} & p.waivable


def test_1hive_repeats_and_checks_code_and_base():
    one = load_policy(POLICY, "1-hive")
    assert one.repeatable == {"code", "base"}
    assert one.ancestry == (("base", "code", "CODE_BASE_INVALID"),)
    assert load_policy(POLICY, "core").repeatable == frozenset()
