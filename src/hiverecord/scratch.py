# SPDX-License-Identifier: GPL-3.0-or-later
"""Scratch hives: a throwaway Postgres in rootless Podman (SPEC §22.1).

The container runs on a tmpfs with ``--rm`` and a label, so ``down`` (or
``down --all``) leaves no container, volume or database behind."""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import time
from dataclasses import dataclass

import psycopg

IMAGE = os.environ.get("HIVE_SCRATCH_IMAGE", "docker.io/library/postgres:17-alpine")
LABEL = "io.github.1-hive.hive-record.scratch=1"


@dataclass
class Scratch:
    name: str
    port: int
    password: str

    @property
    def superuser_url(self) -> str:
        return f"postgresql://postgres:{self.password}@127.0.0.1:{self.port}/postgres"

    def url(self, user: str, password: str, db: str) -> str:
        return f"postgresql://{user}:{password}@127.0.0.1:{self.port}/{db}"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def up(name: str | None = None, timeout: float = 60.0) -> Scratch:
    name = name or f"hive-scratch-{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(16)
    port = _free_port()
    subprocess.run(
        ["podman", "run", "-d", "--rm", "--name", name, "--label", LABEL,
         "-e", f"POSTGRES_PASSWORD={password}", "-p", f"127.0.0.1:{port}:5432",
         "--tmpfs", "/var/lib/postgresql/data", IMAGE, "-c", "fsync=off"],
        check=True, stdout=subprocess.DEVNULL)
    s = Scratch(name, port, password)
    deadline = time.monotonic() + timeout
    while True:
        try:
            with psycopg.connect(s.superuser_url, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            # the entrypoint restarts the server once after initdb; wait for the final one
            time.sleep(0.5)
            with psycopg.connect(s.superuser_url, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            return s
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                down(name)
                raise
            time.sleep(0.3)


def down(name: str) -> None:
    subprocess.run(["podman", "rm", "-f", "-t", "1", name], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def down_all() -> list[str]:
    out = subprocess.run(["podman", "ps", "-a", "-q", "--filter", f"label={LABEL}"],
                         check=True, capture_output=True, text=True).stdout.split()
    for cid in out:
        down(cid)
    return out


def leftovers() -> list[str]:
    return subprocess.run(["podman", "ps", "-a", "-q", "--filter", f"label={LABEL}"],
                          check=True, capture_output=True, text=True).stdout.split()
