# SPDX-License-Identifier: GPL-3.0-or-later
"""Postgres storage (SPEC §18): roles, migrations, gapless append, export, restore."""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources

import psycopg
from psycopg import sql

from .canonical import digest, dumps, loads, sha256_hex
from .engine import request_of

LOCK_KEY = 0x68697665  # "hive": the advisory lock held for every gate-and-append
SCHEMA_VERSION = "1"
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


class StoreError(Exception):
    pass


@dataclass(frozen=True)
class Roles:
    prefix: str = "hive"

    def __post_init__(self) -> None:
        if not ROLE_RE.match(self.prefix):
            raise StoreError(f"bad role prefix {self.prefix!r}")

    @property
    def admin(self) -> str:
        return f"{self.prefix}_admin"

    @property
    def gateway(self) -> str:
        return f"{self.prefix}_gateway"

    @property
    def reader(self) -> str:
        return f"{self.prefix}_reader"


def bootstrap(superuser_url: str, dbname: str, roles: Roles, passwords: dict[str, str] | None = None) -> dict[str, str]:
    """Create the three login roles and a database owned by the admin role.
    Returns the passwords (generated when not given). Needs a superuser."""
    passwords = dict(passwords or {})
    with psycopg.connect(superuser_url, autocommit=True) as conn:
        for role in (roles.admin, roles.gateway, roles.reader):
            pw = passwords.setdefault(role, secrets.token_urlsafe(24))
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            verb = "ALTER" if exists else "CREATE"
            conn.execute(sql.SQL(verb + " ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(pw)))
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(dbname), sql.Identifier(roles.admin)))
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(dbname)))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
            sql.Identifier(dbname), sql.Identifier(roles.gateway), sql.Identifier(roles.reader)))
    return passwords


def _migration_sql(roles: Roles) -> str:
    text = resources.files("hiverecord").joinpath("migrations", "0001_init.sql").read_text()
    for k in ("admin", "gateway", "reader"):
        text = text.replace("{" + k + "}", sql.Identifier(getattr(roles, k)).as_string(None))
    return text


def migrate(conn: psycopg.Connection, roles: Roles) -> None:
    """Create the schema if absent (run as the admin role)."""
    exists = conn.execute("SELECT to_regclass('public.events')").fetchone()[0]
    if exists is None:
        conn.execute(_migration_sql(roles))
        conn.execute("INSERT INTO hive_meta (key, value) VALUES ('schema_version', %s)", (SCHEMA_VERSION,))


def row(ev: dict, goal_id: str | None) -> tuple:
    raw = dumps(ev)
    return (ev["position"], ev["event_id"], ev["recorded_at"], ev["type"], ev["actor"]["id"],
            goal_id, ev.get("task"), ev["idempotency_key"], sha256_hex(dumps(request_of(ev))),
            raw.decode("utf-8"), digest(raw), raw.decode("utf-8"))


INSERT = ("INSERT INTO events (position, event_id, recorded_at, type, actor_id, goal_id, task_id, "
          "idempotency_key, request_digest, canonical, digest, envelope) "
          "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)")


class PgStore:
    """One connection, used under the gateway's lock."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.conn = psycopg.connect(url)

    def close(self) -> None:
        self.conn.close()

    def meta(self) -> dict[str, str]:
        with self.conn.transaction():
            return dict(self.conn.execute("SELECT key, value FROM hive_meta").fetchall())

    def generation(self) -> str | None:
        return self.meta().get("generation")

    def load(self, after: int = 0) -> list[dict]:
        """Events after ``after``, each checked against its stored digest."""
        out = []
        with self.conn.transaction():
            cur = self.conn.execute(
                "SELECT position, canonical, digest FROM events WHERE position > %s ORDER BY position", (after,))
            for pos, canonical, dig in cur:
                raw = canonical.encode("utf-8")
                if digest(raw) != dig:
                    raise StoreError(f"position {pos}: stored digest does not match its canonical bytes")
                out.append(loads(raw))
        return out

    @contextmanager
    def append_tx(self) -> Iterator[tuple[str | None, int]]:
        """A transaction holding the advisory lock. Yields (generation, head)."""
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            gen = self.conn.execute("SELECT value FROM hive_meta WHERE key = 'generation'").fetchone()
            head = self.conn.execute("SELECT coalesce(max(position), 0) FROM events").fetchone()[0]
            yield (gen[0] if gen else None, head)

    def insert(self, events: list[tuple[dict, str | None]]) -> None:
        with self.conn.cursor() as cur:
            cur.executemany(INSERT, [row(ev, goal) for ev, goal in events])


def init_meta(conn: psycopg.Connection, hive: str) -> str:
    """Set the hive id and a fresh generation (admin only). Returns the generation."""
    gen = secrets.token_hex(8)
    conn.execute("INSERT INTO hive_meta (key, value) VALUES ('hive', %s), ('generation', %s)", (hive, gen))
    return gen


def export(conn: psycopg.Connection, after: int = 0) -> Iterator[bytes]:
    cur = conn.execute("SELECT canonical FROM events WHERE position > %s ORDER BY position", (after,))
    for (canonical,) in cur:
        yield canonical.encode("utf-8")


def restore(admin_url: str, roles: Roles, lines: list[bytes]) -> str:
    """Replace the log with an exported one and a new generation (SPEC §18).
    Events after the export are lost. Returns the new generation."""
    events = [loads(raw) for raw in lines]
    if not events or events[0]["type"] != "hive.initialized":
        raise StoreError("an export must start with hive.initialized")
    goals: dict[str, str | None] = {}
    rows = []
    for i, ev in enumerate(events, 1):
        if ev["position"] != i:
            raise StoreError(f"gap at position {i}")
        if ev["type"] == "task.created":
            goals[ev["task"]] = ev.get("goal")
        rows.append((ev, ev.get("goal") or goals.get(ev.get("task"))))
    with psycopg.connect(admin_url) as conn, conn.transaction():
        conn.execute("DROP TABLE IF EXISTS events, hive_meta CASCADE")
        conn.execute("DROP FUNCTION IF EXISTS events_append_only()")
        migrate(conn, roles)
        gen = init_meta(conn, events[0]["hive"])
        with conn.cursor() as cur:
            cur.executemany(INSERT, [row(ev, g) for ev, g in rows])
    return gen
