# SPDX-License-Identifier: GPL-3.0-or-later
"""Canonical encoding (SPEC §7.3) and keys (SPEC §6.3)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hiverecord import keys
from hiverecord.canonical import EncodingError, Ids, dumps, format_time, loads


def test_canonical_bytes():
    assert dumps({"b": 1, "a": [True, None, "é"]}) == '{"a":[true,null,"é"],"b":1}'.encode()


@pytest.mark.parametrize("text", ['{"a":1.5}', '{"a":1e3}', '{"a":NaN}', '{"a":1,"a":2}',
                                  '{"a":9007199254740992}', '{"a":"\\ud800"}', "{"])
def test_non_canonical_input_refused(text):
    with pytest.raises(EncodingError):
        dumps(loads(text))


def test_max_int_allowed():
    assert loads('{"a":9007199254740991}') == {"a": 2**53 - 1}


def test_uuid7_shape_and_determinism():
    at = datetime(2026, 9, 1, tzinfo=UTC)
    a, b = Ids(seed=1).uuid7(at), Ids(seed=1).uuid7(at)
    assert a == b and a[14] == "7" and a[19] in "89ab"
    assert Ids().uuid7(at) != Ids().uuid7(at)


def test_time_format():
    assert format_time(datetime(2026, 9, 1, 12, tzinfo=UTC)) == "2026-09-01T12:00:00.000000Z"


def test_sign_and_verify(tmp_path):
    k = keys.SigningKey.generate()
    msg = keys.write_string("h", "2026-09-01T12:00:00Z", "00" * 32)
    sig = k.sign(msg)
    assert keys.verify(k.public, msg, sig)
    assert not keys.verify(k.public, msg + b"x", sig)
    assert not keys.verify(keys.SigningKey.generate().public, msg, sig)
    assert not keys.verify("ed25519:short", msg, sig)
    path = tmp_path / "k.json"
    k.to_file(path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert keys.SigningKey.from_file(path).public == k.public
    with pytest.raises(FileExistsError):
        k.to_file(path)


def test_read_and_write_strings_differ():
    w = keys.write_string("h", "t", keys.EMPTY_DIGEST)
    r = keys.read_string("h", "t", "GET", "/v1/events")
    assert r.startswith(w + b"\n") and r != w
