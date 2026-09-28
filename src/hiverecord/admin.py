# SPDX-License-Identifier: GPL-3.0-or-later
"""Admin operations run as the admin role: bootstrap a hive (SPEC §19.5)."""

from __future__ import annotations

from pathlib import Path

import psycopg
from hivepin import Config, Registry

from .canonical import Ids, utcnow
from .engine import Engine
from .pins import PinChecker, PolicyResolver, registry_digest
from .store import INSERT, LOCK_KEY, Roles, init_meta, migrate, row


def init_hive(admin_url: str, roles: Roles, *, hive: str, profile: str, mode: str, policy_pin: dict,
              registry_path: str, operator: dict, config: Config | None = None,
              clock=utcnow, ids: Ids | None = None) -> str:
    """Create the schema and generation and append hive.initialized, which also
    registers the first operator with its public key. Refuses a non-empty log.
    Returns the generation."""
    config = config or Config.load()
    registry = Registry.load(Path(registry_path))
    reg_digest = registry_digest(registry_path)
    engine = Engine(PolicyResolver(registry=registry, config=config,
                                   cache_dir=config.cache_dir / "hiverecord"),
                    pins=PinChecker(registry, config), registry_digest=reg_digest, clock=clock, ids=ids)
    with psycopg.connect(admin_url) as conn, conn.transaction():
        migrate(conn, roles)
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        if conn.execute("SELECT count(*) FROM events").fetchone()[0]:
            raise SystemExit("refusing to initialize: the log is not empty")
        ev = engine.initialize(hive=hive, mode=mode, profile=profile, policy_pin=policy_pin,
                               registry_digest=reg_digest, operator=operator)
        conn.execute("DELETE FROM hive_meta WHERE key IN ('hive', 'generation')")
        gen = init_meta(conn, hive)
        conn.execute(INSERT, row(ev, None))
    return gen
