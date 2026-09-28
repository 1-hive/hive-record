# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import pytest

from hiverecord.conformance import FixtureRepo


@pytest.fixture(scope="session")
def pg():
    from hiverecord import scratch
    s = scratch.up()
    yield s
    scratch.down(s.name)


@pytest.fixture(scope="session")
def fx() -> FixtureRepo:
    return FixtureRepo()
