# SPDX-License-Identifier: GPL-3.0-or-later
"""End to end through the `hive` CLI and a gateway process (SPEC §19, §23)."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time

import httpx
import pytest

from hiverecord.canonical import dumps

pytestmark = [pytest.mark.pg,
              pytest.mark.skipif(shutil.which("podman") is None, reason="needs rootless Podman")]
HIVE = [sys.executable, "-m", "hiverecord.cli"]


def run(*args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([*HIVE, *args], capture_output=True, text=True, env=env, check=check)


def test_operator_walkthrough(pg, fx, tmp_path):
    # roles, database, keys
    secrets = tmp_path / "secrets.json"
    run("db-bootstrap", "--superuser-url", pg.superuser_url, "--db", "clihive", "--prefix", "cli",
        "--secrets-file", str(secrets))
    pw = json.loads(secrets.read_text())
    url = {r: pg.url(f"cli_{r}", pw[f"cli_{r}"], "clihive") for r in ("admin", "gateway", "reader")}
    pub = {}
    for a in ("op", "cos", "w1", "rv1"):
        pub[a] = run("keygen", "--out", str(tmp_path / f"{a}.key")).stdout.strip()
        assert pub[a].startswith("ed25519:")
        assert "secret" not in pub[a]
    policy_pin = tmp_path / "policy.pin"
    policy_pin.write_bytes(dumps(fx.policy_pin()))
    pins = {}
    for name in ("goal-1", "order-1", "result-1", "review-1", "summary-1"):
        pins[name] = tmp_path / f"{name}.pin"
        pins[name].write_bytes(dumps(fx.pin(name)))

    # a hive with the 1-hive profile
    env = {**os.environ, "HIVEPIN_CACHE_DIRECTORY": str(fx.root / "cache")}
    out = run("init", "--admin-url", url["admin"], "--prefix", "cli", "--hive", "clihive", "--profile", "1-hive",
              "--policy", str(policy_pin), "--mode", "authoritative", "--registry", str(fx.registry_path),
              "--operator-id", "op", "--operator-key", pub["op"], env=env)
    assert "initialized hive clihive" in out.stdout
    again = run("init", "--admin-url", url["admin"], "--prefix", "cli", "--hive", "clihive", "--profile", "core",
                "--policy", str(policy_pin), "--mode", "authoritative", "--registry", str(fx.registry_path),
                "--operator-id", "op", "--operator-key", pub["op"], env=env, check=False)
    assert again.returncode != 0 and "not empty" in again.stderr

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    gw = subprocess.Popen([*HIVE, "gateway", "--db-url", url["gateway"], "--registry", str(fx.registry_path),
                           "--port", str(port)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(base + "/v1/health").status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.1)
        health = httpx.get(base + "/v1/health").json()
        assert health["hive"] == "clihive" and health["profile"] == "1-hive"

        def hive(actor: str, *args: str, check: bool = True):
            e = {**env, "HIVE_URL": base, "HIVE_ID": "clihive", "HIVE_KEY_FILE": str(tmp_path / f"{actor}.key")}
            return run(*args, env=e, check=check)

        for a, cls in (("cos", "chief_of_staff"), ("w1", "worker"), ("rv1", "reviewer")):
            hive("op", "emit", "actor.registered", "--data", json.dumps(
                {"actor_id": a, "class": cls, "role": cls, "keys": [pub[a]],
                 "declaration": {"harness": "cli", "model_route": "none", "sandbox": a}}))
        hive("cos", "emit", "project.opened", "--data", '{"project":"p1","title":"P1"}')
        hive("cos", "emit", "goal.proposed", "--goal", "g1", "--ref", f"goal={pins['goal-1']}", "--data", json.dumps(
            {"project": "p1", "title": "G1", "objective": "O", "relevance": "R", "budget": {"tokens": 10}}))
        inbox = hive("op", "inbox").stdout
        assert "goal_proposed" in inbox and "g1" in inbox
        refused = hive("cos", "emit", "goal.approved", "--goal", "g1", check=False)
        assert refused.returncode == 1 and "NOT_AUTHORIZED" in refused.stdout
        hive("op", "emit", "goal.approved", "--goal", "g1")
        lease = {"lease": {"accept_within_seconds": 600, "checkin_every_seconds": 900}}
        hive("cos", "emit", "task.created", "--task", "t1", "--goal", "g1", "--ref", f"order={pins['order-1']}",
             "--data", '{"title":"Build","project":"p1"}')
        hive("cos", "emit", "task.assigned", "--task", "t1", "--data", json.dumps({"to": "w1", "ext": lease}))
        hive("w1", "emit", "task.accepted", "--task", "t1")
        ev = json.loads(hive("w1", "emit", "task.result_posted", "--task", "t1",
                             "--ref", f"result={pins['result-1']}").stdout)
        closed = hive("cos", "emit", "task.closed", "--task", "t1", check=False)
        assert "REVIEW_REQUIRED" in closed.stdout
        hive("cos", "emit", "review.assigned", "--task", "t1",
             "--data", json.dumps({"reviewer": "rv1", "result_event": ev["event_id"]}))
        hive("rv1", "emit", "review.recorded", "--task", "t1", "--ref", f"review={pins['review-1']}",
             "--data", json.dumps({"verdict": "passed", "result_event": ev["event_id"]}))
        assert "review:passed" in hive("cos", "board").stdout
        hive("cos", "emit", "task.closed", "--task", "t1")
        hive("cos", "emit", "goal.completed", "--goal", "g1", "--ref", f"summary={pins['summary-1']}",
             "--data", '{"outcome":"done"}')
        hive("op", "emit", "goal.accepted", "--goal", "g1")
        goal = json.loads(hive("op", "goal", "g1").stdout)
        assert goal["goal"]["status"] == "accepted" and goal["tasks"][0]["status"] == "done"

        # export, verify and fold without the database
        export = tmp_path / "export.jsonl"
        export.write_text(hive("op", "export").stdout)
        direct = run("export", "--db-url", url["reader"]).stdout
        assert direct == export.read_text()
        assert "ok" in run("verify-log", str(export), "--policy", str(fx.policy_dir)).stdout
        state = json.loads(run("fold", str(export), "--policy", str(fx.policy_dir)).stdout)
        live = json.loads(hive("op", "state").stdout)
        assert state == live
    finally:
        gw.terminate()
        gw.wait(10)
