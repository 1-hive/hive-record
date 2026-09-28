# SPDX-License-Identifier: GPL-3.0-or-later
"""The gateway: the only writer of the log (SPEC §19)."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from hivepin import Config, Registry
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from . import __version__, projections
from .auth import Unauthenticated, authenticate
from .canonical import Ids, dumps, loads, utcnow
from .engine import Engine
from .pins import PinChecker, PolicyResolver, registry_digest
from .store import PgStore

log = logging.getLogger("hiverecord.gateway")
HARD_BODY = 1024 * 1024
VIA_RE = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,63}$")


def json_response(payload: dict, status: int = 200) -> Response:
    return Response(dumps(payload), status_code=status, media_type="application/json")


@dataclass
class GatewayConfig:
    db_url: str
    registry_path: str
    source_commit: str = "unknown"
    version: str = __version__
    hivepin_config: Config = field(default_factory=Config.load)
    cache_dir: str | None = None


class Gateway:
    def __init__(self, cfg: GatewayConfig, *, clock: Callable = utcnow, ids: Ids | None = None) -> None:
        self.cfg = cfg
        self.clock = clock
        self.ids = ids or Ids()
        self.registry = Registry.load(Path(cfg.registry_path))
        self.registry_digest = registry_digest(cfg.registry_path)
        self.checker = PinChecker(self.registry, cfg.hivepin_config)
        cache = cfg.cache_dir or str(cfg.hivepin_config.cache_dir / "hiverecord")
        self.resolver = PolicyResolver(registry=self.registry, config=cfg.hivepin_config, cache_dir=cache)
        self.lock = threading.Lock()
        self.store = PgStore(cfg.db_url)
        self.engine: Engine
        self.generation: str | None = None
        self.goal_of: list[str | None] = []
        self._load()

    # --- state ----------------------------------------------------------------
    def _new_engine(self) -> Engine:
        return Engine(self.resolver, pins=self.checker, registry_digest=self.registry_digest,
                      clock=self.clock, ids=self.ids)

    def _load(self) -> None:
        engine = self._new_engine()
        self.goal_of = []
        for ev in self.store.load():
            engine.apply(ev)
            self.goal_of.append(self._goal(engine, ev))
        self.engine = engine
        self.generation = self.store.generation()

    @staticmethod
    def _goal(engine: Engine, ev: dict) -> str | None:
        if "goal" in ev:
            return ev["goal"]
        task = engine.state["tasks"].get(ev.get("task")) if ev.get("task") else None
        return task["goal"] if task else None

    def _commit(self, fn: Callable[[Engine], list[dict]]) -> list[dict]:
        """Run ``fn`` (gate + fold) and persist what it appended, atomically,
        under the process lock and the database advisory lock."""
        with self.lock:
            try:
                with self.store.append_tx() as (gen, head):
                    if gen != self.generation or head != self.engine.head:
                        log.warning("log changed underneath the gateway; reloading")
                        self._load()
                    before = self.engine.head
                    events = fn(self.engine)
                    new = [e for e in events if e["position"] > before]
                    self.store.insert([(e, self._goal(self.engine, e)) for e in new])
                    self.goal_of.extend(self._goal(self.engine, e) for e in new)
                return events
            except Exception:
                log.exception("append failed; reloading from the database")
                self._load()
                raise

    def start(self) -> dict:
        """Append gateway.started before admitting any request (SPEC §19.3)."""
        if self.engine.head == 0:
            raise RuntimeError("the log is empty: run `hive init` first")
        data = {"version": self.cfg.version, "source_commit": self.cfg.source_commit,
                "policy_digest": self.engine.state["hive"]["policy"]["content_digest"],
                "registry_digest": self.registry_digest}
        if self.engine.state["hive"]["registry_digest"] != self.registry_digest:
            log.warning("the loaded registry differs from the recorded one; pins will be refused "
                        "until an operator records hive.registry_changed")
        return self._commit(lambda e: [e.append_gateway("gateway.started", data)])[0]

    # --- helpers ----------------------------------------------------------------
    def envelope(self, payload: dict, status: int = 200) -> Response:
        return json_response({**payload, "generation": self.generation, "head": self.engine.head}, status)

    def read_auth(self, request: Request):
        target = request.url.path
        qs = request.scope.get("query_string", b"").decode("latin-1")
        if qs:
            target += "?" + qs
        h = request.headers
        return authenticate(self.engine, key=h.get("x-hive-key"), signed_at=h.get("x-hive-signed-at"),
                            sig=h.get("x-hive-signature"), now=self.clock(), method=request.method, target=target)

    def stale_cursor(self, request: Request) -> bool:
        g = request.headers.get("x-hive-generation")
        return g is not None and g != self.generation

    def _prewarm(self, body: bytes) -> None:
        """Verify pins before taking the lock; results are cached, so the gate's
        own step 7 is then cheap. Errors are left for the gate to report."""
        try:
            req = loads(body)
            for ref in req.get("refs", [])[:16]:
                self.checker(ref["pin"])
        except Exception:
            pass

    # --- endpoints --------------------------------------------------------------
    async def post_event(self, request: Request) -> Response:
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > HARD_BODY:
                log.info("413: body over %d bytes, not recorded", HARD_BODY)
                return self.envelope({"status": "error", "reason": "request body too large"}, 413)
        body = bytes(body)
        h = request.headers
        via = h.get("x-hive-via")
        if via is not None and not VIA_RE.match(via):
            return self.envelope({"status": "error", "reason": "bad X-Hive-Via"}, 400)
        try:
            auth = authenticate(self.engine, key=h.get("x-hive-key"), signed_at=h.get("x-hive-signed-at"),
                                sig=h.get("x-hive-signature"), now=self.clock(), body=body)
        except Unauthenticated as exc:
            log.info("401 on write: %s", exc)
            return self.envelope({"status": "unauthenticated", "reason": str(exc)}, 401)
        await run_in_threadpool(self._prewarm, body)
        holder: dict = {}

        def gate(engine: Engine) -> list[dict]:
            # Re-check the key against the fold under the lock: it may have been
            # revoked since the fast check above.
            if engine.actor_for_key(auth.signature["key"]) != auth.actor:
                return []
            gen_ok = h.get("x-hive-generation") in (None, self.generation)
            out = engine.submit(body, auth, via=via, generation_ok=gen_ok)
            holder["out"] = out
            return out.events

        await run_in_threadpool(self._commit, gate)
        out = holder.get("out")
        if out is None:
            return self.envelope({"status": "unauthenticated", "reason": "key no longer valid"}, 401)
        status = out.http
        if out.status == "refused":
            r = out.refusal
            return self.envelope({"status": "refused", "code": r.code, "reason": r.reason,
                                  "retryable": r.retryable, "refusal": out.events[0]}, status)
        payload = {"status": out.status, "event": out.events[0]}
        if len(out.events) > 1:
            payload["events"] = out.events
        return self.envelope(payload, status)

    async def guarded(self, request: Request, fn) -> Response:
        """Authenticate a read, then run ``fn`` under the lock so it sees a
        consistent fold. ``fn`` is synchronous and must be quick."""
        def run() -> Response:
            with self.lock:
                try:
                    self.read_auth(request)
                except Unauthenticated as exc:
                    log.info("401 on read: %s", exc)
                    return self.envelope({"status": "unauthenticated", "reason": str(exc)}, 401)
                if self.stale_cursor(request):
                    return self.envelope({"status": "refused", "code": "STALE_GENERATION",
                                          "reason": "the log was restored; drop your cursor and re-read"}, 409)
                return fn(request)
        return await run_in_threadpool(run)

    def _int(self, request: Request, name: str, default: int | None) -> int | None:
        v = request.query_params.get(name)
        return default if v is None else int(v)

    async def get_events(self, request: Request) -> Response:
        def fn(request: Request) -> Response:
            after = self._int(request, "after", 0)
            limit = min(self._int(request, "limit", 1000), 1000)
            q = request.query_params
            out = []
            for i, ev in enumerate(self.engine.events[after:], start=after):
                if q.get("type") and ev["type"] != q["type"]:
                    continue
                if q.get("task") and ev.get("task") != q["task"]:
                    continue
                if q.get("goal") and self.goal_of[i] != q["goal"]:
                    continue
                out.append(ev)
                if len(out) >= limit:
                    break
            return self.envelope({"events": out})
        return await self.guarded(request, fn)

    async def subscribe(self, request: Request) -> Response:
        def fn(request: Request) -> Response:
            cursor = int(request.headers.get("last-event-id") or request.query_params.get("after") or 0)
            generation = self.generation

            async def stream():
                nonlocal cursor
                idle = 0.0
                while not await request.is_disconnected():
                    if self.generation != generation:
                        yield b"event: stale_generation\ndata: {}\n\n"
                        return
                    events = self.engine.events
                    if len(events) > cursor:
                        for ev in events[cursor:]:
                            yield b"id: %d\nevent: event\ndata: " % ev["position"] + dumps(ev) + b"\n\n"
                        cursor = len(events)
                        idle = 0.0
                    else:
                        await asyncio.sleep(0.1)
                        idle += 0.1
                        if idle >= 15:
                            yield b": keepalive\n\n"
                            idle = 0.0

            return StreamingResponse(stream(), media_type="text/event-stream",
                                     headers={"X-Hive-Generation": generation or "", "Cache-Control": "no-cache"})
        return await self.guarded(request, fn)

    def state_at(self, at: int | None) -> Engine:
        if at is None or at >= self.engine.head:
            return self.engine
        eng = Engine(self.resolver)
        eng.replay(self.engine.events[:max(at, 0)])
        return eng

    async def get_state(self, request: Request) -> Response:
        def fn(request: Request) -> Response:
            eng = self.state_at(self._int(request, "at", None))
            return self.envelope({"at": eng.head, "state": eng.state})
        return await self.guarded(request, fn)

    async def get_task(self, request: Request) -> Response:
        def fn(request: Request) -> Response:
            view = projections.task_view(self.engine.state, self.engine.events, request.path_params["id"])
            if view is None:
                return self.envelope({"status": "not_found"}, 404)
            return self.envelope(view)
        return await self.guarded(request, fn)

    async def get_goal(self, request: Request) -> Response:
        def fn(request: Request) -> Response:
            view = projections.goal_view(self.engine.state, self.engine.events, request.path_params["id"])
            if view is None:
                return self.envelope({"status": "not_found"}, 404)
            return self.envelope(view)
        return await self.guarded(request, fn)

    async def get_inbox(self, request: Request) -> Response:
        def fn(request: Request) -> Response:
            cls = request.query_params.get("for", "operator")
            items = projections.inbox(self.engine.state, self.engine.policy, cls)
            return self.envelope({"for": cls, "items": items})
        return await self.guarded(request, fn)

    async def get_actors(self, request: Request) -> Response:
        def fn(request: Request) -> Response:
            actors = [{k: a[k] for k in ("id", "class", "role", "status", "keys", "declaration", "role_prompt")}
                      for a in sorted(self.engine.state["actors"].values(), key=lambda a: a["id"])]
            return self.envelope({"actors": actors})
        return await self.guarded(request, fn)

    async def health(self, request: Request) -> Response:
        hive = self.engine.state["hive"] or {}
        pol = self.engine.policy
        return self.envelope({"status": "ok", "hive": hive.get("hive"), "profile": hive.get("profile"),
                              "policy_version": pol.version if pol else None, "gateway_version": __version__})

    def app(self) -> Starlette:
        return Starlette(routes=[
            Route("/v1/events", self.post_event, methods=["POST"]),
            Route("/v1/events", self.get_events, methods=["GET"]),
            Route("/v1/subscribe", self.subscribe, methods=["GET"]),
            Route("/v1/state", self.get_state, methods=["GET"]),
            Route("/v1/tasks/{id}", self.get_task, methods=["GET"]),
            Route("/v1/goals/{id}", self.get_goal, methods=["GET"]),
            Route("/v1/inbox", self.get_inbox, methods=["GET"]),
            Route("/v1/actors", self.get_actors, methods=["GET"]),
            Route("/v1/health", self.health, methods=["GET"]),
        ])

