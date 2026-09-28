# SPDX-License-Identifier: GPL-3.0-or-later
"""Actor keys and signed requests (SPEC §6.3)."""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .canonical import sha256_hex

KEY_RE = re.compile(r"^ed25519:[A-Za-z0-9_-]{43}$")
SIGNED_AT_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,6})?Z$")
EMPTY_DIGEST = sha256_hex(b"{}")


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64d(text: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def valid_key(key: object) -> bool:
    return isinstance(key, str) and bool(KEY_RE.match(key))


def write_string(hive: str, signed_at: str, body_sha256_hex: str) -> bytes:
    return f"hive1\n{hive}\n{signed_at}\n{body_sha256_hex}".encode()


def read_string(hive: str, signed_at: str, method: str, target: str) -> bytes:
    return f"hive1\n{hive}\n{signed_at}\n{EMPTY_DIGEST}\n{method} {target}".encode()


def verify(key: str, message: bytes, sig: str) -> bool:
    if not valid_key(key):
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(b64d(key.split(":", 1)[1]))
        raw = b64d(sig)
        if len(raw) != 64:
            return False
        pub.verify(raw, message)
        return True
    except (ValueError, InvalidSignature):
        return False


@dataclass(frozen=True)
class SigningKey:
    """An ed25519 private key. It never leaves the actor's sandbox (SPEC §6.6)."""

    seed: bytes

    @classmethod
    def generate(cls) -> SigningKey:
        return cls(os.urandom(32))

    @classmethod
    def from_seed_text(cls, text: str) -> SigningKey:
        """Deterministic key for fixtures and tests only."""
        return cls(bytes.fromhex(sha256_hex(("hive-fixture-key:" + text).encode())))

    @property
    def public(self) -> str:
        pub = Ed25519PrivateKey.from_private_bytes(self.seed).public_key().public_bytes_raw()
        return "ed25519:" + b64e(pub)

    def sign(self, message: bytes) -> str:
        return b64e(Ed25519PrivateKey.from_private_bytes(self.seed).sign(message))

    # --- key files -------------------------------------------------------
    def to_file(self, path: str | os.PathLike) -> None:
        data = json.dumps({"type": "ed25519", "public": self.public, "secret": b64e(self.seed)})
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(data + "\n")

    @classmethod
    def from_file(cls, path: str | os.PathLike) -> SigningKey:
        data = json.loads(Path(path).read_text())
        if data.get("type") != "ed25519":
            raise ValueError(f"{path}: unsupported key type {data.get('type')!r}")
        key = cls(b64d(data["secret"]))
        if key.public != data.get("public"):
            raise ValueError(f"{path}: public key does not match secret")
        return key
