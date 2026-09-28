# SPDX-License-Identifier: GPL-3.0-or-later
"""A signed HTTP client for the gateway (SPEC §6.3, §19)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from .auth import sign_read, sign_write
from .canonical import dumps
from .keys import SigningKey


def signed_at_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class HiveError(Exception):
    def __init__(self, status: int, payload: dict) -> None:
        super().__init__(f"HTTP {status}: {payload.get('code') or payload.get('reason') or payload}")
        self.status = status
        self.payload = payload


class Client:
    """``url`` is ``http://host:port`` or ``unix:///path/to.sock``."""

    def __init__(self, url: str, hive: str, key: SigningKey, *, via: str | None = None,
                 timeout: float = 30.0, transport: httpx.BaseTransport | None = None) -> None:
        self.hive = hive
        self.key = key
        self.via = via
        self.head: int | None = None
        self.generation: str | None = None
        if url.startswith("unix://"):
            transport = transport or httpx.HTTPTransport(uds=url[len("unix://"):])
            url = "http://hive"
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=timeout, transport=transport)

    def close(self) -> None:
        self.http.close()

    def _note(self, payload: dict) -> None:
        if isinstance(payload, dict):
            self.head = payload.get("head", self.head)
            self.generation = payload.get("generation", self.generation)

    # --- reads -------------------------------------------------------------------
    def get(self, path: str, params: dict | None = None) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        target = path + ("?" + urlencode(params) if params else "")
        headers = sign_read(self.key, self.hive, signed_at_now(), "GET", target)
        r = self.http.get(target, headers=headers)
        payload = r.json()
        if r.status_code != 200:
            raise HiveError(r.status_code, payload)
        self._note(payload)
        return payload

    def health(self) -> dict:
        r = self.http.get("/v1/health")
        payload = r.json()
        self._note(payload)
        return payload

    def events(self, after: int = 0, limit: int = 1000, **filters: str | None) -> list[dict]:
        return self.get("/v1/events", {"after": after, "limit": limit, **filters})["events"]

    def all_events(self, after: int = 0, **filters: str | None) -> Iterator[dict]:
        while True:
            batch = self.events(after, 1000, **filters)
            if not batch:
                return
            yield from batch
            after = batch[-1]["position"]

    def state(self, at: int | None = None) -> dict:
        return self.get("/v1/state", {"at": at})["state"]

    def task(self, tid: str) -> dict:
        return self.get(f"/v1/tasks/{tid}")

    def goal(self, gid: str) -> dict:
        return self.get(f"/v1/goals/{gid}")

    def inbox(self, for_class: str = "operator") -> list[dict]:
        return self.get("/v1/inbox", {"for": for_class})["items"]

    def actors(self) -> list[dict]:
        return self.get("/v1/actors")["actors"]

    def subscribe(self, after: int = 0) -> Iterator[dict]:
        target = f"/v1/subscribe?{urlencode({'after': after})}"
        headers = sign_read(self.key, self.hive, signed_at_now(), "GET", target)
        with self.http.stream("GET", target, headers=headers, timeout=None) as r:
            if r.status_code != 200:
                raise HiveError(r.status_code, json.loads(r.read() or b"{}"))
            event = data = None
            for line in r.iter_lines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data = line[6:]
                elif line == "" and event:
                    if event == "stale_generation":
                        raise HiveError(409, {"code": "STALE_GENERATION"})
                    if event == "event" and data:
                        yield json.loads(data)
                    event = data = None

    # --- writes ------------------------------------------------------------------
    def post(self, request: dict) -> tuple[int, dict]:
        body = dumps(request)
        headers = {**sign_write(self.key, self.hive, signed_at_now(), body), "Content-Type": "application/json"}
        if self.via:
            headers["X-Hive-Via"] = self.via
        if self.generation:
            headers["X-Hive-Generation"] = self.generation
        r = self.http.post("/v1/events", content=body, headers=headers)
        payload = r.json()
        self._note(payload)
        return r.status_code, payload

    def emit(self, type_: str, *, data: dict | None = None, refs: list[dict] | None = None,
             task: str | None = None, goal: str | None = None, expected_revision: int | None = None,
             idempotency_key: str | None = None, basis: int | None = None) -> dict:
        """Submit a request; raise HiveError unless it is accepted (or a duplicate)."""
        if basis is None:
            if self.head is None:
                self.health()
            basis = self.head
        req = {"type": type_, "basis": basis, "idempotency_key": idempotency_key or new_key(),
               "refs": refs or [], "data": data or {}}
        if task is not None:
            req["task"] = task
        if goal is not None:
            req["goal"] = goal
        if expected_revision is not None:
            req["expected_revision"] = expected_revision
        status, payload = self.post(req)
        if status not in (200, 201):
            raise HiveError(status, payload)
        return payload


def new_key() -> str:
    import secrets
    return "cli:" + secrets.token_hex(12)
