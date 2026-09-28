# SPDX-License-Identifier: GPL-3.0-or-later
"""Policy trees: load, compose and validate a profile (SPEC §10, §13)."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path

from hivepin.canonical import manifest_digest
from jsonschema import Draft202012Validator

from . import vocab
from .canonical import sha256_hex

RESERVED_PREFIXES = ("message.", "skill.", "route.", "trial.", "memory.")
GATEWAY_TYPES = ("hive.initialized", "gateway.started", "gateway.rejected")


class PolicyError(Exception):
    """A policy tree or profile that must not be loaded."""


@cache
def meta_schema(name: str) -> dict:
    """A schema from the top-level ``schemas/`` directory (installed or in-tree)."""
    try:
        text = resources.files("hiverecord").joinpath("_schemas", name).read_text()
    except (FileNotFoundError, NotADirectoryError):
        text = (Path(__file__).resolve().parents[2] / "schemas" / name).read_text()
    return json.loads(text)


@cache
def meta_validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(meta_schema(name))


def schema_errors(validator: Draft202012Validator, value: object) -> str | None:
    err = next(iter(sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))), None)
    if err is None:
        return None
    where = "/".join(str(p) for p in err.absolute_path)
    return f"{where or '(root)'}: {err.message}"[:500]


def tree_digest(root: Path) -> str:
    """The R0 content digest of a directory, as a tree pin of it would record."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            full = Path(dirpath) / name
            st = full.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise PolicyError(f"policy tree contains a non-regular file: {full}")
            data = full.read_bytes()
            entries.append({
                "path": full.relative_to(root).as_posix(),
                "mode": "100755" if st.st_mode & 0o111 else "100644",
                "size": len(data),
                "digest": "sha256:" + sha256_hex(data),
            })
    return manifest_digest(entries)


@dataclass(frozen=True)
class Rule:
    event: str
    entity: str
    classes: frozenset[str] | None          # None = any class except gateway
    relation: str | None
    frm: object                             # None | "*" | "open" | tuple of states
    to: object                              # state | "same" | {"by", "map"}
    required: tuple[str, ...]
    allowed: tuple[str, ...]
    conditions: tuple[tuple[str, tuple[str, ...]], ...]
    effects: tuple[str, ...]
    require_ext: tuple[str, ...] = ()
    require_goal: bool = False
    core: bool = True
    exceptable: bool = False

    def permits(self, cls: str) -> bool:
        if self.classes is None:
            return cls != "gateway"
        return cls in self.classes


@dataclass
class Policy:
    name: str
    version: str
    core_version: str
    digest: str | None
    classes: tuple[str, ...]
    rels: frozenset[str]
    codes: frozenset[str]
    entities: dict[str, dict]
    rules: dict[str, Rule]
    data_schemas: dict[str, Draft202012Validator]
    ext_schemas: dict[str, Draft202012Validator]
    inbox: dict[str, tuple[str, ...]]
    hooks: dict[str, tuple[str, ...]] = field(default_factory=dict)
    waivable: frozenset[str] = frozenset()

    def entity_of(self, event_type: str) -> dict:
        return self.entities[self.rules[event_type].entity]


def _cond(c: object) -> tuple[str, tuple[str, ...]]:
    if isinstance(c, str):
        return (c, ())
    return (c["name"], tuple(c.get("args", ())))


def _rule(r: dict, *, core: bool) -> Rule:
    frm = r["from"]
    return Rule(
        event=r["event"],
        entity=r["entity"],
        classes=None if r["classes"] == "*" else frozenset(r["classes"]),
        relation=r["relation"],
        frm=tuple(frm) if isinstance(frm, list) else frm,
        to=r["to"],
        required=tuple(r["refs"]["required"]),
        allowed=tuple(r["refs"]["allowed"]),
        conditions=tuple(_cond(c) for c in r["conditions"]),
        effects=tuple(r["effects"]),
        core=core,
        exceptable=r.get("exceptable", False),
    )


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise PolicyError(f"{path}: {exc}") from None


def _schemas(directory: Path) -> dict[str, Draft202012Validator]:
    out = {}
    if directory.is_dir():
        for p in sorted(directory.glob("*.schema.json")):
            sch = _read_json(p)
            try:
                Draft202012Validator.check_schema(sch)
            except Exception as exc:
                raise PolicyError(f"{p}: invalid JSON Schema: {exc}") from None
            out[p.name.removesuffix(".schema.json")] = Draft202012Validator(sch)
    return out


def load_policy(root: str | os.PathLike, profile: str, *, digest: str | None = None) -> Policy:
    """Load ``<root>/core`` and, unless ``profile == "core"``, compose
    ``<root>/profiles/<profile>`` onto it. Raises PolicyError on any violation."""
    root = Path(root)
    table = _read_json(root / "core" / "legality.json")
    err = schema_errors(meta_validator("legality-table-v1.schema.json"), table)
    if err:
        raise PolicyError(f"core/legality.json: {err}")

    rules: dict[str, Rule] = {}
    for r in table["rules"]:
        if r["event"] in rules:
            raise PolicyError(f"core: two rules for {r['event']}")
        rules[r["event"]] = _rule(r, core=True)

    classes = list(table["classes"])
    rels = set(table["rels"])
    codes = set(table["codes"])
    entities = dict(table["entities"])
    data_schemas = _schemas(root / "core" / "schemas" / "events")
    ext_schemas: dict[str, Draft202012Validator] = {}
    inbox = {k: tuple(v) for k, v in table["inbox"].items()}
    hooks: dict[str, tuple[str, ...]] = {}
    waivable = set(table["waivable_codes"])
    name, version = "core", table["version"]

    if profile != "core":
        pdir = root / "profiles" / profile
        if not (pdir / "profile.json").is_file():
            raise PolicyError(f"no profile {profile!r} in the policy tree")
        prof = _read_json(pdir / "profile.json")
        err = schema_errors(meta_validator("profile-v1.schema.json"), prof)
        if err:
            raise PolicyError(f"profile {profile}: {err}")
        if prof["name"] != profile:
            raise PolicyError(f"profile directory {profile!r} declares name {prof['name']!r}")
        name, version = prof["name"], prof["version"]
        _compose(prof, rules, classes, rels, codes, entities)
        for t, v in _schemas(pdir / "schemas" / "events").items():
            if t in data_schemas:
                raise PolicyError(f"profile {profile}: redefines the schema of core type {t}")
            data_schemas[t] = v
        ext_schemas = _schemas(pdir / "schemas" / "ext")
        inbox = {k: tuple(v) for k, v in prof["inbox"].items()}
        hooks = {k: tuple(v) for k, v in prof.get("hooks", {}).items()}
        extra = set(prof.get("waivable_codes", []))
        if not extra <= set(prof["add_codes"]):
            raise PolicyError(f"profile {profile}: may only make its own codes waivable, "
                              f"not {sorted(extra - set(prof['add_codes']))}")
        waivable |= extra

    policy = Policy(
        name=name, version=version, core_version=table["version"], digest=digest,
        classes=tuple(classes), rels=frozenset(rels), codes=frozenset(codes),
        entities=entities, rules=rules, data_schemas=data_schemas, ext_schemas=ext_schemas,
        inbox=inbox, hooks=hooks, waivable=frozenset(waivable),
    )
    _validate(policy)
    return policy


def _compose(prof: dict, rules: dict[str, Rule], classes: list[str], rels: set[str],
             codes: set[str], entities: dict[str, dict]) -> None:
    """Apply an extension with the add-or-tighten rules of SPEC §13.3."""
    pname = prof["name"]
    new_classes = set(prof["add_classes"])
    if new_classes & set(classes):
        raise PolicyError(f"profile {pname}: re-adds core classes {sorted(new_classes & set(classes))}")
    classes.extend(prof["add_classes"])
    rels.update(prof["add_rels"])
    codes.update(prof["add_codes"])
    for ename, e in prof["entities"].items():
        if ename in entities:
            raise PolicyError(f"profile {pname}: redefines entity {ename!r}")
        entities[ename] = e

    for r in prof["rules"]:
        t = r["event"]
        if t in rules:
            raise PolicyError(f"profile {pname}: defines a rule for existing type {t} (amend it instead)")
        if t.startswith(RESERVED_PREFIXES):
            raise PolicyError(f"profile {pname}: {t} uses a reserved prefix")
        rule = _rule(r, core=False)
        core_entity = rule.entity in ("hive", "project", "actor", "task")
        if core_entity:
            # A new event on a core entity may not move it or touch core state.
            if rule.frm is None or rule.to != "same":
                raise PolicyError(f"profile {pname}: {t} must not create or move a core entity")
            bad = [e for e in rule.effects if vocab.EFFECTS.get(e, (None,))[0] != "ext"]
            if bad:
                raise PolicyError(f"profile {pname}: {t} uses non-extension effects {bad}")
        rules[t] = rule

    for t, a in prof["amend"].items():
        if t not in rules or not rules[t].core:
            raise PolicyError(f"profile {pname}: amends {t}, which is not a core type")
        base = rules[t]
        add_classes = a.get("add_classes", [])
        if not set(add_classes) <= new_classes:
            raise PolicyError(f"profile {pname}: {t} grants core classes "
                              f"{sorted(set(add_classes) - new_classes)} (only new classes allowed)")
        effects = a.get("add_effects", [])
        bad = [e for e in effects if vocab.EFFECTS.get(e, (None,))[0] != "ext"]
        if bad:
            raise PolicyError(f"profile {pname}: {t} adds non-extension effects {bad}")
        refs = a.get("add_refs", {"required": [], "allowed": []})
        rules[t] = Rule(
            event=base.event, entity=base.entity,
            classes=None if base.classes is None else base.classes | frozenset(add_classes),
            relation=base.relation, frm=base.frm, to=base.to,
            required=base.required + tuple(refs["required"]),
            allowed=base.allowed + tuple(refs["allowed"]),
            conditions=base.conditions + tuple(_cond(c) for c in a.get("add_conditions", [])),
            effects=base.effects + tuple(effects),
            require_ext=base.require_ext + tuple(a.get("require_ext", [])),
            require_goal=base.require_goal or a.get("require_goal", False),
            core=True,
            exceptable=base.exceptable and not a.get("forbid_exceptions", False),
        )


def _validate(p: Policy) -> None:
    """Every name a table uses must exist; every type has a schema and vice versa."""
    for code in vocab.ENGINE_CODES:
        if code not in p.codes:
            raise PolicyError(f"code {code} used by the engine is not declared")
    for ename, e in p.entities.items():
        for k in ("unknown_code", "exists_code", "illegal_code"):
            if e[k] not in p.codes:
                raise PolicyError(f"entity {ename}: unknown {k} {e[k]}")
        if not set(e["terminal"]) <= set(e["states"]):
            raise PolicyError(f"entity {ename}: terminal states not among its states")
    for code in p.waivable:
        if code not in p.codes:
            raise PolicyError(f"waivable code {code} is not declared")
    for t, r in p.rules.items():
        where = f"rule {t}"
        if r.exceptable and r.entity in ("hive", "project", "actor", "exception"):
            raise PolicyError(f"{where}: only task and extension rules may be exceptable")
        if r.entity not in p.entities:
            raise PolicyError(f"{where}: unknown entity {r.entity}")
        ent = p.entities[r.entity]
        if r.classes is not None and not r.classes <= set(p.classes):
            raise PolicyError(f"{where}: unknown classes {sorted(r.classes - set(p.classes))}")
        states = set(ent["states"])
        if isinstance(r.frm, tuple) and not set(r.frm) <= states:
            raise PolicyError(f"{where}: unknown from-states")
        targets = r.to["map"].values() if isinstance(r.to, dict) else [r.to]
        for s in targets:
            if s != "same" and s not in states:
                raise PolicyError(f"{where}: unknown to-state {s}")
        if r.frm is None and r.to == "same":
            raise PolicyError(f"{where}: a creating rule needs a to-state")
        for rel in r.required + r.allowed:
            if rel not in p.rels:
                raise PolicyError(f"{where}: unknown rel {rel}")
        for name, _args in r.conditions:
            if name not in vocab.CONDITIONS:
                raise PolicyError(f"{where}: unknown condition {name}")
            for code in vocab.CONDITIONS[name][1]:
                if code not in p.codes:
                    raise PolicyError(f"{where}: condition {name} raises undeclared code {code}")
        for e in r.effects:
            if e not in vocab.EFFECTS:
                raise PolicyError(f"{where}: unknown effect {e}")
        if t not in p.data_schemas:
            raise PolicyError(f"{where}: no data schema")
        for f in r.require_ext:
            if t not in p.ext_schemas:
                raise PolicyError(f"{where}: require_ext without an ext schema")
            if f not in p.ext_schemas[t].schema.get("properties", {}):
                raise PolicyError(f"{where}: require_ext field {f} not in its ext schema")
    for t in p.data_schemas:
        if t not in p.rules:
            raise PolicyError(f"schema for {t} has no rule")
    for t in p.ext_schemas:
        if t not in p.rules or not p.rules[t].core:
            raise PolicyError(f"ext schema for {t}, which is not a core type")
        if "ext" not in p.data_schemas[t].schema.get("properties", {}):
            raise PolicyError(f"ext schema for {t}, which does not allow ext")
    for cls, kinds in p.inbox.items():
        for k in kinds:
            if k not in vocab.INBOX_RULES:
                raise PolicyError(f"inbox for {cls}: unknown rule {k}")
    for hook, effects in p.hooks.items():
        for e in effects:
            if vocab.EFFECTS.get(e, (None,))[0] != "ext":
                raise PolicyError(f"hook {hook}: {e} is not an extension effect")
