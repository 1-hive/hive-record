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


def test_stream_model_reads_the_last_assistant_message(tmp_path):
    import json

    from hiverecord.cli import stream_model
    f = tmp_path / "s.jsonl"
    lines = [{"type": "system", "model": "x"},
             {"type": "assistant", "message": {"model": "claude-opus-5-5"}},
             {"type": "assistant", "parent_tool_use_id": "toolu_1", "message": {"model": "claude-haiku-5-5"}},
             {"type": "assistant", "message": {"model": "<synthetic>"}},
             {"type": "user", "message": {}}]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    assert stream_model(str(f)) == "claude-haiku-5-5"   # the message that ran the command, a subagent's too
    assert stream_model(str(tmp_path / "missing")) is None
    f.write_text(json.dumps({"type": "result", "result": "x"}) + "\n")
    assert stream_model(str(f)) is None


def test_emit_model_order(tmp_path, monkeypatch):
    import argparse
    import json

    from hiverecord.cli import emit_model
    f = tmp_path / "s.jsonl"
    f.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-5-5"}}) + "\n")
    monkeypatch.setenv("HIVE_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("HIVE_MODEL_STREAM", str(f))
    assert emit_model(argparse.Namespace(model=None)) == "claude-sonnet-5-5"
    assert emit_model(argparse.Namespace(model="gpt-5.5")) == "gpt-5.5"
    assert emit_model(argparse.Namespace(model="")) is None
    monkeypatch.setenv("HIVE_MODEL_STREAM", str(tmp_path / "missing"))
    assert emit_model(argparse.Namespace(model=None)) == "claude-opus-5-5"
    monkeypatch.delenv("HIVE_MODEL")
    assert emit_model(argparse.Namespace(model=None)) is None


def test_takes_model_only_where_the_profile_does():
    from hiverecord.cli import takes_model

    class C:
        def __init__(self, profile, version):
            self.h = {"profile": profile, "policy_version": version}

        def health(self):
            return self.h
    assert takes_model(C("1-hive", "1.2.0"), "task.result_posted")
    assert takes_model(C("proto-hive", "1.1.0"), "task.blocked")
    assert not takes_model(C("1-hive", "1.1.0"), "task.result_posted")    # before A7: never sent, never refused
    assert not takes_model(C("proto-hive", "1.0.0"), "review.recorded")
    assert not takes_model(C("core", "1.3.0"), "task.reported")
    assert not takes_model(C("1-hive", "1.2.0"), "task.closed")
