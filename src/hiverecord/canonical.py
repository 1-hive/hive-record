# SPDX-License-Identifier: GPL-3.0-or-later
"""Canonical encoding (SPEC §7.3): R0 canonical JSON, integers only."""

from __future__ import annotations

import hashlib
import json
import os
import random
from datetime import UTC, datetime

MAX_INT = 2**53 - 1


class EncodingError(ValueError):
    """Input that has no canonical form. Callers map it to SCHEMA_INVALID."""


def _reject_float(text: str) -> object:
    raise EncodingError(f"non-integer number {text!r}")


def _reject_constant(text: str) -> object:
    raise EncodingError(f"{text} is not JSON")


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise EncodingError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _check_ints(value: object) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        if abs(value) > MAX_INT:
            raise EncodingError("integer out of range")
    elif isinstance(value, dict):
        for v in value.values():
            _check_ints(v)
    elif isinstance(value, list):
        for v in value:
            _check_ints(v)
    else:
        raise EncodingError(f"unsupported value of type {type(value).__name__}")


def loads(data: bytes | str) -> object:
    """Strict parse: no floats, NaN, duplicate keys or out-of-range integers."""
    try:
        value = json.loads(
            data,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicates,
        )
    except EncodingError:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise EncodingError(f"invalid JSON: {exc}") from None
    _check_ints(value)
    return value


def dumps(value: object) -> bytes:
    """Canonical bytes: UTF-8, code-point key order, no whitespace, no trailing LF."""
    _check_ints(value)
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EncodingError(f"string is not valid Unicode: {exc}") from None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(data: bytes) -> str:
    return "sha256:" + sha256_hex(data)


# --------------------------------------------------------------------------- #
# time and ids
# --------------------------------------------------------------------------- #
def format_time(dt: datetime) -> str:
    """RFC 3339 UTC with microseconds, e.g. 2026-09-27T10:00:00.000000Z."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_time(text: str) -> datetime:
    if not isinstance(text, str) or not text.endswith("Z"):
        raise ValueError(f"not an RFC 3339 UTC time: {text!r}")
    return datetime.fromisoformat(text[:-1] + "+00:00")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Ids:
    """UUIDv7 generator. Deterministic when seeded (fixtures), random otherwise."""

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed) if seed is not None else None

    def _bits(self, n: int) -> int:
        if self._rng is not None:
            return self._rng.getrandbits(n)
        return int.from_bytes(os.urandom((n + 7) // 8), "big") >> (-n % 8)

    def uuid7(self, at: datetime) -> str:
        ms = int(at.timestamp() * 1000) & ((1 << 48) - 1)
        value = (ms << 80) | (0x7 << 76) | (self._bits(12) << 64) | (0b10 << 62) | self._bits(62)
        h = f"{value:032x}"
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
