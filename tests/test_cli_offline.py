# SPDX-License-Identifier: GPL-3.0-or-later
"""The standalone `hive fold` / `hive verify-log` commands, without a database (SPEC §16.3)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HIVE = [sys.executable, "-m", "hiverecord.cli"]
LOG = Path(__file__).resolve().parents[1] / "fixtures" / "core" / "policy-change" / "log.jsonl"


def run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([*HIVE, *args], capture_output=True, text=True, env=env)


def test_verify_log_with_registry_materializes_pinned_trees(fx):
    env = {**os.environ, "HIVEPIN_CACHE_DIRECTORY": str(fx.root / "cache")}
    out = run("verify-log", str(LOG), "--registry", str(fx.registry_path), env=env)
    assert out.returncode == 0, out.stderr
    assert "ok:" in out.stdout


def test_missing_policy_tree_is_a_clean_error(tmp_path):
    out = run("verify-log", str(LOG), "--policy", str(tmp_path))
    assert out.returncode != 0
    assert "Traceback" not in out.stderr
    assert "no local policy tree" in out.stderr and "--registry" in out.stderr


def test_policy_or_registry_is_required():
    out = run("fold", str(LOG))
    assert out.returncode != 0 and "--policy DIR" in out.stderr
