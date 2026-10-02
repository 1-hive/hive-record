# SPDX-License-Identifier: GPL-3.0-or-later
"""Conformance fixtures (SPEC §21): the fixture repository, and a runner that
replays ``requests.jsonl`` against an engine with a deterministic clock and ids."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import hivepin
from hivepin import Config, Registry

from . import keys
from .auth import Unauthenticated, authenticate
from .canonical import Ids, digest, dumps, loads, parse_time, sha256_hex
from .engine import Engine
from .pins import PinChecker, PolicyResolver
from .policy import tree_digest

REPO = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(os.environ.get("HIVE_FIXTURE_ROOT", "/tmp/hive-record-fixture"))


def actor_key(seed: str) -> keys.SigningKey:
    """Fixture actors' keys are derived from their ids. Never use outside tests."""
    return keys.SigningKey.from_seed_text(seed)


class FixtureRepo:
    """The repository built by ``fixtures/make-repo.sh``, rebuilt when the policy
    tree in this checkout changes."""

    def __init__(self, root: Path = FIXTURE_ROOT, policy_src: Path = REPO / "policy") -> None:
        self.root = Path(root)
        script = REPO / "fixtures" / "make-repo.sh"
        stamp = self.root / "make-repo.sha256"
        want = sha256_hex(script.read_bytes())
        if not (self.root / "registry.json").is_file() or (
            tree_digest(self.root / "fixture" / "policy") != tree_digest(policy_src)
        ) or not stamp.is_file() or stamp.read_text() != want:
            subprocess.run([str(script), str(self.root)], check=True, stdout=subprocess.DEVNULL)
            stamp.write_text(want)
        self.registry_path = self.root / "registry.json"
        self.registry = Registry.load(self.registry_path)
        self.registry_digest = digest(dumps(loads(self.registry_path.read_bytes())))
        self.config = Config(cache_directory=str(self.root / "cache"))
        self.policy_dir = self.root / "fixture" / "policy"
        self._pins: dict[tuple, dict] = {}

    def _mint(self, path: str | None, commit: str, version: int = 1) -> dict:
        """The published fixtures use v1 pins; v2 pins are minted on demand."""
        key = (path, commit, version)
        if key not in self._pins:
            res = hivepin.mint("fixture", path, self.registry, commit=commit, offline=True,
                               config=self.config, version=version)
            self._pins[key] = res.pin.to_canonical_dict()
        return self._pins[key]

    def pin_v2(self, path: str | None = None, commit: str = "main") -> dict:
        return self._mint(path, commit, version=2)

    def pin(self, name: str) -> dict:
        return self._mint(f"content/{name}.md", "main")

    def policy_pin(self) -> dict:
        return self._mint("policy", "main")

    def unpublished_pin(self) -> dict:
        return self._mint("content/unpublished.md", "wip")

    def checker(self) -> PinChecker:
        if not hasattr(self, "_checker"):
            self._checker = PinChecker(self.registry, self.config)
        return self._checker

    def resolver(self) -> PolicyResolver:
        return PolicyResolver(dirs=[self.policy_dir], registry=self.registry, config=self.config,
                              cache_dir=self.root / "cache")


def default_declaration(actor: str) -> dict:
    return {"harness": "fixture", "model_route": "none", "sandbox": f"fixture:{actor}"}


@dataclass
class Runner:
    """Replays steps against an in-memory engine. Every value it adds (basis,
    signatures, times) is written into the step, so the steps replay exactly."""

    fx: FixtureRepo
    engine: Engine = field(init=False)
    now: datetime = field(init=False)
    steps: list[dict] = field(default_factory=list)
    responses: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.now = parse_time("2026-09-01T12:00:00Z")
        self.engine = Engine(self.fx.resolver(), pins=self.fx.checker(),
                             registry_digest=self.fx.registry_digest, clock=lambda: self.now, ids=Ids(seed=0))

    # --- running ------------------------------------------------------------
    def run(self, step: dict) -> dict:
        self.now = parse_time(step["at"])
        op = step["op"]
        if op == "init":
            ev = self.engine.initialize(hive=step["hive"], mode=step["mode"], profile=step["profile"],
                                        policy_pin=step["policy"], registry_digest=step["registry_digest"],
                                        operator=step["operator"])
            resp = {"status": "accepted", "http": 201, "positions": [ev["position"]]}
        elif op == "start":
            ev = self.engine.append_gateway("gateway.started", step["data"])
            resp = {"status": "accepted", "http": 201, "positions": [ev["position"]]}
        elif op == "request":
            resp = self._request(step)
        else:
            raise ValueError(f"unknown op {op!r}")
        self.steps.append(step)
        self.responses.append(resp)
        return resp

    def _request(self, step: dict) -> dict:
        body = self.body_of(step)
        signer = actor_key(step.get("signer", step["actor"]))
        signed_at = step.get("signed_at", step["at"])
        sig = signer.sign(keys.write_string(step.get("sign_hive", self.engine.hive_id), signed_at, sha256_hex(body)))
        try:
            auth = authenticate(self.engine, key=signer.public, signed_at=signed_at, sig=sig,
                                now=self.now, body=body)
        except Unauthenticated:
            return {"status": "unauthenticated", "http": 401}
        out = self.engine.submit(body, auth, via=step.get("via"), generation_ok=step.get("generation") != "stale")
        resp = {"status": out.status, "http": out.http, "positions": [e["position"] for e in out.events]}
        if out.refusal:
            resp["code"] = out.refusal.code
        return resp

    @staticmethod
    def body_of(step: dict) -> bytes:
        if "body" in step:
            return step["body"].encode("utf-8")
        req = step["request"]
        if "pad" in step:
            req = {**req, "data": {**req["data"], "pad": "x" * step["pad"]}}
        return dumps(req)

    # --- files ---------------------------------------------------------------
    def log_lines(self) -> bytes:
        return b"".join(dumps(e) + b"\n" for e in self.engine.events)


def read_jsonl(path: Path) -> list[dict]:
    return [loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_bytes(b"".join(dumps(r) + b"\n" for r in rows))


def fixture_dirs(root: Path = REPO / "fixtures") -> list[Path]:
    return sorted(p.parent for p in root.glob("*/*/requests.jsonl"))


def replay_fixture(fx: FixtureRepo, d: Path) -> Runner:
    runner = Runner(fx)
    for step in read_jsonl(d / "requests.jsonl"):
        runner.run(step)
    return runner


def fold_log(lines: list[dict], policy_dirs: list[Path]) -> Engine:
    """The standalone fold (SPEC §16.3): no database, no network."""
    engine = Engine(PolicyResolver(dirs=policy_dirs))
    engine.replay(lines)
    return engine


def advance(at: str, seconds: int = 60) -> str:
    return (parse_time(at) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pretty(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
