# SPDX-License-Identifier: GPL-3.0-or-later
"""Request authentication (SPEC §6.3, §6.4). Failures are never recorded."""

from __future__ import annotations

from datetime import datetime

from . import keys
from .canonical import parse_time, sha256_hex
from .engine import Auth, Engine

MAX_SKEW_SECONDS = 300


class Unauthenticated(Exception):
    """HTTP 401. Logged by the service, never recorded in the log."""


def authenticate(engine: Engine, *, key: str | None, signed_at: str | None, sig: str | None,
                 now: datetime, body: bytes | None = None, method: str = "POST",
                 target: str = "") -> Auth:
    """Resolve the signing key to its actor. ``body`` is given for writes; reads
    sign the method and target instead."""
    if not key or not signed_at or not sig:
        raise Unauthenticated("missing signature headers")
    if not keys.valid_key(key):
        raise Unauthenticated("malformed key")
    if not keys.SIGNED_AT_RE.match(signed_at):
        raise Unauthenticated("malformed signed-at")
    skew = abs((now - parse_time(signed_at)).total_seconds())
    if skew > MAX_SKEW_SECONDS:
        raise Unauthenticated(f"signed-at is {int(skew)}s from the gateway clock")
    hive = engine.hive_id
    if body is not None:
        message = keys.write_string(hive, signed_at, sha256_hex(body))
    else:
        message = keys.read_string(hive, signed_at, method, target)
    if not keys.verify(key, message, sig):
        raise Unauthenticated("bad signature")
    actor = engine.actor_for_key(key)
    if actor is None:
        raise Unauthenticated("unknown, revoked or retired key")
    return Auth(actor=actor, signature={"key": key, "signed_at": signed_at, "sig": sig})


def sign_write(key: keys.SigningKey, hive: str, signed_at: str, body: bytes) -> dict[str, str]:
    return {"X-Hive-Key": key.public, "X-Hive-Signed-At": signed_at,
            "X-Hive-Signature": key.sign(keys.write_string(hive, signed_at, sha256_hex(body)))}


def sign_read(key: keys.SigningKey, hive: str, signed_at: str, method: str, target: str) -> dict[str, str]:
    return {"X-Hive-Key": key.public, "X-Hive-Signed-At": signed_at,
            "X-Hive-Signature": key.sign(keys.read_string(hive, signed_at, method, target))}
