# SPDX-License-Identifier: GPL-3.0-or-later
"""Pin admission and policy resolution through hivepin (SPEC §8, §10)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import hivepin
from hivepin import Config, PinError, Registry, parse_pin, pin_from_dict

from .canonical import digest, dumps, loads
from .errors import Refusal
from .policy import Policy, PolicyError, load_policy, tree_digest


def registry_digest(path: str | os.PathLike) -> str:
    """sha256 of the registry file's canonical JSON (SPEC §8)."""
    return digest(dumps(loads(Path(path).read_bytes())))


class PinChecker:
    """``hivepin.verify`` with the publication check, caching successes for the life
    of the process (a verified pin stays verified: its content is immutable)."""

    def __init__(self, registry: Registry, config: Config) -> None:
        self.registry = registry
        self.config = config
        self._ok: set[bytes] = set()

    def __call__(self, pin: dict) -> None:
        key = dumps(pin)
        if key in self._ok:
            return
        try:
            hivepin.verify(pin_from_dict(pin), self.registry, offline=False, config=self.config)
        except PinError as exc:
            raise _refusal(exc) from None
        self._ok.add(key)

    def is_ancestor(self, ancestor: dict, descendant: dict) -> bool:
        """Whether one v2 pin's commit is an ancestor of another's (same repository)."""
        try:
            return hivepin.is_ancestor(pin_from_dict(ancestor), pin_from_dict(descendant),
                                       self.registry, offline=False, config=self.config)
        except PinError as exc:
            raise _refusal(exc) from None


def _refusal(exc: PinError) -> Refusal:
    if exc.code == "REMOTE_UNAVAILABLE":
        return Refusal("PIN_UNAVAILABLE", exc.message, hivepin_code=exc.code)
    return Refusal("PIN_INVALID", exc.message, hivepin_code=exc.code)


class PolicyResolver:
    """Maps a policy pin to a loaded, composed :class:`Policy`.

    A pinned tree is found in ``dirs`` (local directories whose R0 tree digest
    matches the pin, e.g. ``hive fold --policy``) or materialized through hivepin
    into ``cache_dir``. Either way the bytes are checked against the pin, so the
    fold depends only on the log and the trees it pins (invariant 9)."""

    def __init__(self, *, dirs: list[str | os.PathLike] = (), registry: Registry | None = None,
                 config: Config | None = None, cache_dir: str | os.PathLike | None = None) -> None:
        self.dirs = {}
        for d in dirs:
            self.dirs[tree_digest(Path(d))] = Path(d)
        self.registry = registry
        self.config = config
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._loaded: dict[tuple[str, str], Policy] = {}

    def __call__(self, pin: dict, profile: str) -> Policy:
        key = (_policy_key(pin), profile)
        if key in self._loaded:
            return self._loaded[key]
        if pin.get("version") == 2:
            if "path" not in pin:
                raise PolicyError("a v2 policy pin must name the policy directory (its path)")
            root = self._materialize_v2(pin)
        else:
            if pin.get("kind") != "tree":
                raise PolicyError("the policy pin must be a tree pin")
            root = self.dirs.get(pin.get("content_digest")) or self._materialize(pin)
        policy = load_policy(root, profile, digest=tree_digest(root))
        self._loaded[key] = policy
        return policy

    def _materialize(self, pin: dict) -> Path:
        if self.registry is None or self.cache_dir is None:
            raise PolicyError(f"no local policy tree matches {pin.get('content_digest')}")
        dest = self.cache_dir / "policies" / pin["content_digest"].split(":", 1)[1]
        # hivepin keeps the pinned path under the destination
        root = dest if pin["path"] == "." else dest / pin["path"]
        if root.is_dir() and tree_digest(root) == pin["content_digest"]:
            return root
        self._extract(pin, dest)
        if tree_digest(root) != pin["content_digest"]:
            raise PolicyError("the materialized policy tree does not match its pin")
        return root

    def _materialize_v2(self, pin: dict) -> Path:
        """A v2 policy pin names a commit, not a digest, so no --policy dir can match
        it: it is materialized through hivepin, which re-hashes every object."""
        if self.registry is None or self.cache_dir is None:
            raise PolicyError(f"the v2 policy pin {_label(pin)} needs a registry to resolve")
        dest = self.cache_dir / "policies" / ("v2-" + digest(dumps(pin)).split(":", 1)[1])
        self._extract(pin, dest)    # always re-extracted: nothing on disk is trusted
        return dest / pin["path"]

    def _extract(self, pin: dict, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=dest.parent)) / "tree"
        try:
            res = hivepin.materialize(pin_from_dict(pin), tmp, self.registry, offline=True,
                                      config=self.config)
            if getattr(res, "omitted", ()):
                raise PolicyError(f"the policy tree has entries hivepin did not create: "
                                  f"{[o['path'] for o in res.omitted]}")
            if dest.exists():
                shutil.rmtree(dest)
            tmp.rename(dest)
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)


def _policy_key(pin: dict) -> str:
    return pin.get("content_digest") or digest(dumps(pin))


def _label(pin: dict) -> str:
    try:
        return pin_from_dict(pin).display_label
    except PinError:
        return "(invalid pin)"


def load_pin(text: str) -> dict:
    """A v1 or v2 pin from its canonical JSON or its ``hivepin:v1:``/``v2:`` envelope."""
    text = text.strip()
    pin = parse_pin(text) if text.startswith("hivepin:") else pin_from_dict(json.loads(text))
    return pin.to_canonical_dict()
