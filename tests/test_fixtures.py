# SPDX-License-Identifier: GPL-3.0-or-later
"""Conformance fixtures (SPEC §21): replay, standalone fold, signatures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiverecord import projections
from hiverecord.canonical import dumps
from hiverecord.conformance import fixture_dirs, fold_log, read_jsonl, replay_fixture
from hiverecord.engine import Engine
from hiverecord.pins import PolicyResolver
from hiverecord.verify import verify_log

DIRS = fixture_dirs()
IDS = [f"{d.parent.name}/{d.name}" for d in DIRS]


def expected_states(d: Path) -> dict[int, dict]:
    return {int(p.stem.split("@")[1]): json.loads(p.read_text()) for p in (d / "expected").glob("state@*.json")}


def test_all_fixtures_present():
    names = set(IDS)
    for n in ("happy-path", "review-loop", "stale-review", "block-answer", "reassign", "policy-change",
              "mirror-mode", "mode-flip", "exception", "refusals"):
        assert f"core/{n}" in names
    for n in ("goal-happy-path", "proposals", "escalations", "reassign-restart", "budget", "refusals"):
        assert f"1-hive/{n}" in names


@pytest.mark.parametrize("d", DIRS, ids=IDS)
def test_replay_matches_golden(fx, d):
    runner = replay_fixture(fx, d)
    assert runner.responses == read_jsonl(d / "expected" / "responses.jsonl")
    assert runner.log_lines() == (d / "log.jsonl").read_bytes()


@pytest.mark.parametrize("d", DIRS, ids=IDS)
def test_standalone_fold_matches_expected(fx, d):
    events = read_jsonl(d / "log.jsonl")
    for pos, state in expected_states(d).items():
        eng = fold_log([e for e in events if e["position"] <= pos], [fx.policy_dir])
        assert json.loads(dumps(eng.state)) == state
        inbox = json.loads((d / "expected" / f"inbox@{pos}.json").read_text())
        assert {c: projections.inbox(eng.state, eng.policy, c) for c in eng.policy.inbox} == inbox


@pytest.mark.parametrize("d", DIRS, ids=IDS)
def test_fold_is_deterministic(fx, d):
    events = read_jsonl(d / "log.jsonl")
    a = fold_log(events, [fx.policy_dir]).state_bytes()
    b = fold_log(events, [fx.policy_dir]).state_bytes()
    assert a == b


@pytest.mark.parametrize("d", DIRS, ids=IDS)
def test_every_signature_reverifies_from_the_log(fx, d):
    lines = (d / "log.jsonl").read_bytes().splitlines()
    assert verify_log(lines, Engine(PolicyResolver(dirs=[fx.policy_dir]))) == []


def test_tampered_log_is_detected(fx):
    d = next(x for x in DIRS if x.name == "happy-path")
    lines = (d / "log.jsonl").read_bytes().splitlines()
    ev = json.loads(lines[-1])
    ev["data"]["note"] = "forged"
    lines[-1] = dumps(ev)
    problems = verify_log(lines, Engine(PolicyResolver(dirs=[fx.policy_dir])))
    assert problems and "signature does not verify" in problems[0]


def test_refusals_cover_every_reachable_core_code():
    rs = read_jsonl(next(d for d in DIRS if d.parent.name == "core" and d.name == "refusals")
                    / "expected" / "responses.jsonl")
    codes = {r["code"] for r in rs if "code" in r}
    table = json.loads((Path(__file__).parents[1] / "policy" / "core" / "legality.json").read_text())
    unreachable = {"UNSUPPORTED_SCHEMA", "PIN_UNAVAILABLE", "INTERNAL_ERROR"}  # tested elsewhere or reserved
    assert set(table["codes"]) - unreachable <= codes


def test_refusals_cover_every_reachable_extension_code():
    rs = read_jsonl(next(d for d in DIRS if d.parent.name == "1-hive" and d.name == "refusals")
                    / "expected" / "responses.jsonl")
    others = [read_jsonl(d / "expected" / "responses.jsonl") for d in DIRS if d.parent.name == "1-hive"]
    codes = {r["code"] for group in [rs, *others] for r in group if "code" in r}
    prof = json.loads((Path(__file__).parents[1] / "policy" / "profiles" / "1-hive" / "profile.json").read_text())
    assert set(prof["add_codes"]) <= codes
