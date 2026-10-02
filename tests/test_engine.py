# SPDX-License-Identifier: GPL-3.0-or-later
"""Engine cases the fixtures don't reach: remote-down pins, log integrity."""

from __future__ import annotations

import json

import pytest
from hivepin import Config, Registry

from hiverecord.canonical import dumps, loads
from hiverecord.conformance import (
    actor_key,
    default_declaration,
    fixture_dirs,
    fold_log,
    read_jsonl,
)
from hiverecord.engine import Engine, LogError
from hiverecord.errors import Refusal
from hiverecord.pins import PinChecker, PolicyResolver
from hiverecord.policy import PolicyError


def test_remote_down_is_retryable(fx, tmp_path):
    data = loads(fx.registry_path.read_bytes())
    data["repositories"]["fixture"]["fetch_urls"] = [f"file://{tmp_path}/gone.git"]
    reg = Registry.from_dict(data)
    checker = PinChecker(reg, Config(cache_directory=str(tmp_path / "cache")))
    with pytest.raises(Refusal) as err:
        checker(fx.pin("order-1"))
    assert err.value.code == "PIN_UNAVAILABLE" and err.value.retryable
    assert err.value.detail["hivepin_code"] == "REMOTE_UNAVAILABLE"


def test_policy_materializes_through_hivepin(fx, tmp_path):
    resolver = PolicyResolver(registry=fx.registry, config=fx.config, cache_dir=tmp_path)
    policy = resolver(fx.policy_pin(), "1-hive")
    assert policy.name == "1-hive" and policy.digest == fx.policy_pin()["content_digest"]


def test_v2_policy_pin_needs_a_path(fx, tmp_path):
    resolver = PolicyResolver(registry=fx.registry, config=fx.config, cache_dir=tmp_path)
    policy = resolver(fx.pin_v2("policy"), "1-hive")
    assert policy.name == "1-hive" and policy.digest == fx.policy_pin()["content_digest"]
    with pytest.raises(PolicyError, match="must name the policy directory"):
        resolver(fx.pin_v2(None), "1-hive")
    with pytest.raises(PolicyError, match="needs a registry"):
        PolicyResolver(dirs=[fx.policy_dir])(fx.pin_v2("policy"), "1-hive")


def test_v2_policy_pin_initializes_a_hive(fx):
    eng = Engine(fx.resolver(), pins=fx.checker(), registry_digest=fx.registry_digest)
    eng.initialize(hive="h", mode="authoritative", profile="1-hive", policy_pin=fx.pin_v2("policy"),
                   registry_digest=fx.registry_digest,
                   operator={"id": "op", "role": "owner", "keys": [actor_key("op").public],
                             "declaration": default_declaration("op")})
    assert eng.policy.name == "1-hive" and eng.state["hive"]["policy"] == fx.pin_v2("policy")


def test_ancestry_is_checked_at_admission(fx):
    checker = fx.checker()
    base, tip = fx.pin_v2(None, "main~1"), fx.pin_v2(None, "main")
    assert checker.is_ancestor(base, tip) and not checker.is_ancestor(tip, base)


def test_fold_rejects_impossible_logs(fx):
    d = next(x for x in fixture_dirs() if x.name == "happy-path")
    events = read_jsonl(d / "log.jsonl")
    with pytest.raises(LogError):                       # a gap
        fold_log(events[:3] + events[4:], [fx.policy_dir])
    with pytest.raises(LogError):                       # does not start with hive.initialized
        fold_log([{**e, "position": i + 1} for i, e in enumerate(events[1:])], [fx.policy_dir])
    bad = json.loads(dumps(events))
    bad[5]["task"] = "never-created"
    bad[5]["type"] = "task.accepted"
    with pytest.raises(LogError):
        fold_log(bad, [fx.policy_dir])


def test_fold_needs_the_pinned_policy(tmp_path):
    d = next(x for x in fixture_dirs() if x.name == "happy-path")
    with pytest.raises(Exception, match="no local policy tree"):
        Engine(PolicyResolver(dirs=[])).replay(read_jsonl(d / "log.jsonl"))
