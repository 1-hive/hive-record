# SPDX-License-Identifier: GPL-3.0-or-later
"""The gateway on Postgres (SPEC §18, §19, §22.1). Needs rootless Podman."""

from __future__ import annotations

import itertools
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import psycopg
import pytest
import uvicorn
from starlette.testclient import TestClient

from hiverecord import keys, scratch
from hiverecord.admin import init_hive
from hiverecord.auth import sign_read, sign_write
from hiverecord.canonical import Ids, dumps, loads, parse_time, sha256_hex, utcnow
from hiverecord.client import Client
from hiverecord.conformance import (
    FixtureRepo,
    Runner,
    actor_key,
    default_declaration,
    fixture_dirs,
    read_jsonl,
)
from hiverecord.server import Gateway, GatewayConfig
from hiverecord.store import Roles, bootstrap, export, restore

pytestmark = [pytest.mark.pg,
              pytest.mark.skipif(shutil.which("podman") is None, reason="needs rootless Podman")]
_counter = itertools.count()


@dataclass
class Hive:
    roles: Roles
    db: str
    urls: dict[str, str]
    gw: Gateway
    now: list
    fx: FixtureRepo

    def client(self) -> TestClient:
        return TestClient(self.gw.app())

    def set_time(self, at: str) -> None:
        self.now[0] = parse_time(at)

    def new_gateway(self) -> Gateway:
        return make_gateway(self.fx, self.urls["gateway"], self.now, Ids())


def make_gateway(fx: FixtureRepo, url: str, now: list, ids: Ids) -> Gateway:
    cfg = GatewayConfig(db_url=url, registry_path=str(fx.registry_path), source_commit="0" * 40,
                        version="fixture", hivepin_config=fx.config, cache_dir=str(fx.root / "cache"))
    return Gateway(cfg, clock=lambda: now[0], ids=ids)


def new_db(pg) -> tuple[Roles, str, dict[str, str]]:
    n = next(_counter)
    roles, db = Roles(f"h{n}"), f"hive{n}"
    pw = bootstrap(pg.superuser_url, db, roles)
    urls = {k: pg.url(getattr(roles, k), pw[getattr(roles, k)], db) for k in ("admin", "gateway", "reader")}
    return roles, db, urls


def make_hive(pg, fx, init_step: dict, *, ids: Ids | None = None, start_step: dict | None = None) -> Hive:
    roles, db, urls = new_db(pg)
    ids = ids or Ids()
    now = [parse_time(init_step["at"])]
    init_hive(urls["admin"], roles, hive=init_step["hive"], profile=init_step["profile"], mode=init_step["mode"],
              policy_pin=init_step["policy"], registry_path=str(fx.registry_path), operator=init_step["operator"],
              config=fx.config, clock=lambda: now[0], ids=ids)
    gw = make_gateway(fx, urls["gateway"], now, ids)
    if start_step:
        now[0] = parse_time(start_step["at"])
    gw.start()
    return Hive(roles, db, urls, gw, now, fx)


def default_init(fx, profile="core", mode="authoritative", at="2026-09-01T12:00:00Z") -> dict:
    return {"op": "init", "at": at, "hive": "fixture", "mode": mode, "profile": profile,
            "policy": fx.policy_pin(), "registry_digest": fx.registry_digest,
            "operator": {"id": "op", "role": "owner", "keys": [actor_key("op").public],
                         "declaration": default_declaration("op")}}


def post(hive: Hive, tc: TestClient, actor: str, req: dict | None = None, *, body: bytes | None = None,
         signer: str | None = None, signed_at: str | None = None, sign_hive: str | None = None,
         generation: str | None = None, via: str | None = None):
    body = dumps(req) if body is None else body
    k = actor_key(signer or actor)
    at = signed_at or hive.now[0].strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = sign_write(k, sign_hive or "fixture", at, body)
    if generation:
        headers["X-Hive-Generation"] = generation
    if via:
        headers["X-Hive-Via"] = via
    return tc.post("/v1/events", content=body, headers=headers)


def get(hive: Hive, tc: TestClient, actor: str, target: str, generation: str | None = None):
    at = hive.now[0].strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = sign_read(actor_key(actor), "fixture", at, "GET", target)
    if generation:
        headers["X-Hive-Generation"] = generation
    return tc.get(target, headers=headers)


def db_log(url: str) -> bytes:
    with psycopg.connect(url) as conn:
        return b"".join(raw + b"\n" for raw in export(conn))


# --------------------------------------------------------------------------- #
# fixtures through the real gateway
# --------------------------------------------------------------------------- #
DIRS = fixture_dirs()


@pytest.mark.parametrize("d", DIRS, ids=[f"{d.parent.name}/{d.name}" for d in DIRS])
def test_fixture_through_gateway(pg, fx, d):
    steps = read_jsonl(d / "requests.jsonl")
    assert steps[0]["op"] == "init" and steps[1]["op"] == "start"
    hive = make_hive(pg, fx, steps[0], ids=Ids(seed=0), start_step=steps[1])
    responses = [{"status": "accepted", "http": 201, "positions": [1]},
                 {"status": "accepted", "http": 201, "positions": [2]}]
    with hive.client() as tc:
        for step in steps[2:]:
            hive.set_time(step["at"])
            r = post(hive, tc, step["actor"], body=Runner.body_of(step), signer=step.get("signer"),
                     signed_at=step.get("signed_at"), sign_hive=step.get("sign_hive"),
                     generation="stale" if step.get("generation") == "stale" else None, via=step.get("via"))
            p = r.json()
            if r.status_code == 401:
                responses.append({"status": "unauthenticated", "http": 401})
                continue
            if p["status"] == "refused":
                resp = {"status": "refused", "http": r.status_code, "positions": [p["refusal"]["position"]],
                        "code": p["code"]}
            else:
                evs = p.get("events") or [p["event"]]
                resp = {"status": p["status"], "http": r.status_code, "positions": [e["position"] for e in evs]}
            responses.append(resp)
    assert responses == read_jsonl(d / "expected" / "responses.jsonl")
    golden = (d / "log.jsonl").read_bytes()
    assert b"".join(dumps(e) + b"\n" for e in hive.gw.engine.events) == golden
    assert db_log(hive.urls["reader"]) == golden
    # a fresh gateway process folds the stored log to the same state
    again = hive.new_gateway()
    assert again.engine.state_bytes() == hive.gw.engine.state_bytes()


# --------------------------------------------------------------------------- #
# storage guarantees
# --------------------------------------------------------------------------- #
@pytest.fixture
def hive(pg, fx) -> Hive:
    h = make_hive(pg, fx, default_init(fx))
    h.set_time("2026-09-01T12:05:00Z")
    with h.client() as tc:
        for a, c in (("coord", "coordinator"), ("w1", "worker"), ("rv1", "reviewer")):
            r = post(h, tc, "op", {"type": "actor.registered", "basis": h.gw.engine.head, "idempotency_key": f"r-{a}",
                                   "refs": [], "data": {"actor_id": a, "class": c, "role": c,
                                                        "keys": [actor_key(a).public],
                                                        "declaration": default_declaration(a)}})
            assert r.status_code == 201, r.json()
    return h


def _sql_fails(url: str, stmt: str) -> bool:
    try:
        with psycopg.connect(url) as conn:
            conn.execute(stmt)
        return False
    except psycopg.Error:
        return True


def test_roles_and_append_only_trigger(hive):
    u = hive.urls
    assert _sql_fails(u["reader"], "INSERT INTO hive_meta VALUES ('x', 'y')")
    assert _sql_fails(u["reader"], "DELETE FROM events")
    assert _sql_fails(u["gateway"], "UPDATE events SET type = 'x'")
    assert _sql_fails(u["gateway"], "DELETE FROM events")
    assert _sql_fails(u["gateway"], "UPDATE hive_meta SET value = 'x' WHERE key = 'generation'")
    assert _sql_fails(u["gateway"], "CREATE TABLE sneaky (x int)")
    # the owner is blocked by the trigger too
    assert _sql_fails(u["admin"], "UPDATE events SET type = 'x'")
    assert _sql_fails(u["admin"], "DELETE FROM events")
    assert _sql_fails(u["admin"], "TRUNCATE events")
    before = db_log(u["reader"])
    assert before.count(b"\n") == hive.gw.engine.head


def test_gateway_started_on_every_start(hive):
    g2 = hive.new_gateway()
    ev = g2.start()
    assert ev["type"] == "gateway.started" and ev["data"]["version"] == "fixture"
    assert g2.engine.state["hive"]["gateway"]["position"] == ev["position"]


def _worker_task_request(hive, i: int, key: str | None = None) -> dict:
    return {"type": "project.opened", "basis": 0, "idempotency_key": key or f"p{i}", "refs": [],
            "data": {"project": f"p{i}", "title": f"P{i}"}}


def test_gapless_under_concurrent_writers_and_two_gateways(hive):
    """Two gateway processes on one database, many threads: positions stay gapless."""
    g2 = hive.new_gateway()
    apps = [TestClient(hive.gw.app()), TestClient(g2.app())]
    for tc in apps:
        tc.__enter__()
    try:
        def send(i):
            return post(hive, apps[i % 2], "coord", _worker_task_request(hive, i)).status_code
        with ThreadPoolExecutor(8) as pool:
            codes = list(pool.map(send, range(40)))
    finally:
        for tc in apps:
            tc.__exit__(None, None, None)
    assert codes.count(201) == 40
    log = [loads(line) for line in db_log(hive.urls["reader"]).splitlines()]
    assert [e["position"] for e in log] == list(range(1, len(log) + 1))
    assert sum(e["type"] == "project.opened" for e in log) == 40


def test_idempotent_retries_under_concurrency(hive):
    with hive.client() as tc:
        req = _worker_task_request(hive, 99, key="same")
        with ThreadPoolExecutor(8) as pool:
            rs = list(pool.map(lambda _: post(hive, tc, "coord", req), range(16)))
    assert sorted({r.status_code for r in rs}) == [200, 201]
    assert sum(r.status_code == 201 for r in rs) == 1
    assert len({r.json()["event"]["event_id"] for r in rs}) == 1
    log = [loads(line) for line in db_log(hive.urls["reader"]).splitlines()]
    assert sum(e.get("idempotency_key") == "same" for e in log) == 1


def test_body_cannot_claim_another_actor(hive):
    with hive.client() as tc:
        req = {**_worker_task_request(hive, 1), "actor": {"id": "op", "class": "operator"}}
        r = post(hive, tc, "coord", req)
    assert r.status_code == 422 and r.json()["code"] == "SCHEMA_INVALID"
    assert r.json()["refusal"]["data"]["by"] == {"id": "coord", "class": "coordinator"}


def test_bad_signatures_are_401_and_unrecorded(hive):
    head = hive.gw.engine.head
    req = _worker_task_request(hive, 1)
    with hive.client() as tc:
        assert post(hive, tc, "stranger", req).status_code == 401                     # unknown key
        assert post(hive, tc, "coord", req, signed_at="2026-09-01T11:00:00Z").status_code == 401  # expired
        assert post(hive, tc, "coord", req, sign_hive="elsewhere").status_code == 401  # wrong message
        body = dumps(req)
        h = sign_write(actor_key("coord"), "fixture", "2026-09-01T12:05:00Z", body)
        for bad in ({"X-Hive-Signature": "!!"}, {"X-Hive-Key": "ed25519:short"},
                    {"X-Hive-Signed-At": "yesterday"}, {"X-Hive-Key": ""}):
            assert tc.post("/v1/events", content=body, headers={**h, **bad}).status_code == 401
        assert tc.post("/v1/events", content=body).status_code == 401
        # revoked key
        r = post(hive, tc, "op", {"type": "actor.key_revoked", "basis": 0, "idempotency_key": "rv",
                                  "refs": [], "data": {"actor_id": "w1", "key": actor_key("w1").public,
                                                       "reason": "test"}})
        assert r.status_code == 201
        head = hive.gw.engine.head
        assert post(hive, tc, "w1", req).status_code == 401
        # reads too
        assert tc.get("/v1/events").status_code == 401
        assert get(hive, tc, "w1", "/v1/events").status_code == 401
    assert hive.gw.engine.head == head
    assert db_log(hive.urls["reader"]).count(b"\n") == head


def test_oversized_bodies(hive):
    with hive.client() as tc:
        big = dumps({**_worker_task_request(hive, 1), "data": {"project": "x", "title": "y" * 70_000}})
        r = post(hive, tc, "coord", body=big)
        assert r.status_code == 413 and r.json()["code"] == "PAYLOAD_TOO_LARGE"
        assert r.json()["refusal"]["data"]["refused"] is None
        head = hive.gw.engine.head
        huge = b"x" * (1024 * 1024 + 10)
        assert post(hive, tc, "coord", body=huge).status_code == 413
        assert hive.gw.engine.head == head                                             # not recorded


def test_reads(hive):
    with hive.client() as tc:
        h = tc.get("/v1/health").json()
        assert h["status"] == "ok" and h["hive"] == "fixture" and h["profile"] == "core"
        r = get(hive, tc, "coord", "/v1/events?after=2&limit=2")
        assert r.status_code == 200
        assert [e["position"] for e in r.json()["events"]] == [3, 4]
        assert r.json()["head"] == hive.gw.engine.head and r.json()["generation"]
        r = get(hive, tc, "coord", "/v1/events?type=actor.registered")
        assert len(r.json()["events"]) == 3
        actors = get(hive, tc, "w1", "/v1/actors").json()["actors"]
        assert {a["id"] for a in actors} == {"op", "coord", "w1", "rv1"}
        s = get(hive, tc, "w1", "/v1/state?at=3").json()
        assert s["at"] == 3 and set(s["state"]["actors"]) == {"op", "coord"}
        assert get(hive, tc, "w1", "/v1/inbox?for=operator").json()["items"] == []
        assert get(hive, tc, "w1", "/v1/tasks/nope").status_code == 404


def test_restore_changes_generation(hive, fx):
    with hive.client() as tc:
        gen = get(hive, tc, "coord", "/v1/events").json()["generation"]
    lines = db_log(hive.urls["reader"]).splitlines()[:4]
    new_gen = restore(hive.urls["admin"], hive.roles, lines)
    assert new_gen != gen
    g2 = hive.new_gateway()
    assert g2.engine.head == 4 and g2.generation == new_gen
    with TestClient(g2.app()) as tc:
        assert get(hive, tc, "coord", "/v1/events?after=3", generation=gen).status_code == 409
        r = post(hive, tc, "coord", _worker_task_request(hive, 5), generation=gen)
        assert r.status_code == 409 and r.json()["code"] == "STALE_GENERATION"
        assert get(hive, tc, "coord", "/v1/events", generation=new_gen).status_code == 200
    # the old gateway notices on its next write and reloads
    with hive.client() as tc:
        r = post(hive, tc, "coord", _worker_task_request(hive, 6))
        assert r.status_code == 201 and r.json()["generation"] == new_gen


def test_registry_mismatch_refuses_pins(hive, fx, tmp_path):
    reg = tmp_path / "registry.json"
    data = loads(fx.registry_path.read_bytes())
    data["repositories"]["other"] = {"fetch_urls": ["file:///nonexistent.git"]}
    reg.write_bytes(dumps(data))
    cfg = GatewayConfig(db_url=hive.urls["gateway"], registry_path=str(reg), source_commit="x",
                        hivepin_config=fx.config, cache_dir=str(fx.root / "cache"))
    g2 = Gateway(cfg, clock=lambda: hive.now[0])
    with TestClient(g2.app()) as tc:
        r = post(hive, tc, "coord", {"type": "task.created", "task": "t1", "basis": 0, "idempotency_key": "t1",
                                     "refs": [{"rel": "order", "pin": fx.pin("order-1")}],
                                     "data": {"title": "T"}})
        assert r.json()["code"] == "PIN_UNAVAILABLE"
        assert r.json()["refusal"]["data"]["detail"]["registry_mismatch"] is True
        r = post(hive, tc, "op", {"type": "hive.registry_changed", "basis": 0, "idempotency_key": "rc", "refs": [],
                                  "data": {"registry_digest": g2.registry_digest, "reason": "added a repo"}})
        assert r.status_code == 201
        r = post(hive, tc, "coord", {"type": "task.created", "task": "t1", "basis": 0, "idempotency_key": "t1b",
                                     "refs": [{"rel": "order", "pin": fx.pin("order-1")}],
                                     "data": {"title": "T"}})
        assert r.status_code == 201


# --------------------------------------------------------------------------- #
# a real server: SSE and the client
# --------------------------------------------------------------------------- #
@pytest.fixture
def served(hive):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    hive.gw.clock = utcnow
    hive.gw.engine.clock = hive.gw.clock
    server = uvicorn.Server(uvicorn.Config(hive.gw.app(), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(5)


def test_client_and_sse_resume(served, hive, fx):
    c = Client(served, "fixture", actor_key("coord"))
    head = c.health()["head"]
    got: list[int] = []

    def listen(after, n, sink):
        for ev in Client(served, "fixture", actor_key("w1")).subscribe(after):
            sink.append(ev["position"])
            if len(sink) >= n:
                return

    t = threading.Thread(target=listen, args=(0, head + 3, got), daemon=True)
    t.start()
    for i in range(3):
        c.emit("project.opened", data={"project": f"sse{i}", "title": "x"})
    t.join(10)
    assert got == list(range(1, head + 4))            # each event exactly once, in order
    resumed: list[int] = []
    t = threading.Thread(target=listen, args=(head + 1, 2, resumed), daemon=True)
    t.start()
    t.join(10)
    assert resumed == [head + 2, head + 3]
    # the client sends expected_revision and reads back
    c.emit("task.created", task="t1", data={"title": "T"}, refs=[{"rel": "order", "pin": fx.pin("order-1")}])
    rev = c.task("t1")["task"]["revision"]
    c.emit("task.assigned", task="t1", data={"to": "w1"}, expected_revision=rev)
    with pytest.raises(Exception) as err:
        c.emit("task.cancelled", task="t1", data={"reason": "x"}, expected_revision=rev)
    assert err.value.payload["code"] == "STALE_REVISION"
    assert [e["type"] for e in c.all_events(task="t1")] == ["task.created", "task.assigned"]


def test_scratch_leaves_nothing_behind():
    def names() -> list[str]:
        return subprocess.run(["podman", "ps", "-a", "--format", "{{.Names}}"],
                              capture_output=True, text=True, check=True).stdout.split()
    s = scratch.up()
    assert s.name in names()
    scratch.down(s.name)
    assert s.name not in names()
    vols = subprocess.run(["podman", "volume", "ls", "-q", "--filter", f"label={scratch.LABEL}"],
                          capture_output=True, text=True, check=True).stdout.split()
    assert vols == []


def test_signature_helpers_agree():
    k = keys.SigningKey.generate()
    body = b"{}"
    h = sign_write(k, "hv", "2026-09-01T12:00:00Z", body)
    assert keys.verify(k.public, keys.write_string("hv", "2026-09-01T12:00:00Z", sha256_hex(body)),
                       h["X-Hive-Signature"])
