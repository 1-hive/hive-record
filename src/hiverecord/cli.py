# SPDX-License-Identifier: GPL-3.0-or-later
"""``hive``: the command-line client and admin tool (SPEC §16, §19)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__, projections
from .canonical import dumps, loads

ENV_HELP = """environment:
  HIVE_URL        gateway URL: http://127.0.0.1:8470 or unix:///path/to/gateway.sock
  HIVE_ID         hive id
  HIVE_KEY_FILE   this actor's private key file (from `hive keygen`)
  HIVE_VIA        optional channel attribution (X-Hive-Via)
"""


def _client(args):
    from .client import Client
    from .keys import SigningKey
    missing = [v for v in ("HIVE_URL", "HIVE_ID", "HIVE_KEY_FILE") if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"set {', '.join(missing)} (see `hive --help`)")
    return Client(os.environ["HIVE_URL"], os.environ["HIVE_ID"],
                  SigningKey.from_file(os.environ["HIVE_KEY_FILE"]), via=os.environ.get("HIVE_VIA"))


def _out(value) -> None:
    sys.stdout.write(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def _read_pin(text: str) -> dict:
    from .pins import load_pin
    p = Path(text)
    return load_pin(p.read_text() if p.is_file() else text)


def _policy_engine_for(policy_dirs: list[str] | None, registry: str | None = None):
    """An engine that resolves the policy trees a log pins: from local --policy dirs,
    and, with --registry, by materializing any other pinned tree through hivepin."""
    from .engine import Engine
    from .pins import PolicyResolver
    if not policy_dirs and not registry:
        raise SystemExit("hive: give --policy DIR (repeatable) or --registry FILE")
    if not registry:
        return Engine(PolicyResolver(dirs=[Path(d) for d in policy_dirs or []]))
    from hivepin import Config, Registry
    config = Config.load()
    return Engine(PolicyResolver(dirs=[Path(d) for d in policy_dirs or []], registry=Registry.load(Path(registry)),
                                 config=config, cache_dir=config.cache_dir / "hiverecord"))


def _read_log(path: str) -> list[bytes]:
    data = sys.stdin.buffer.read() if path == "-" else Path(path).read_bytes()
    return [line for line in data.splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_keygen(args) -> None:
    from .keys import SigningKey
    key = SigningKey.generate()
    key.to_file(args.out)
    print(key.public)


def cmd_db_bootstrap(args) -> None:
    from .store import Roles, bootstrap
    pw = bootstrap(args.superuser_url, args.db, Roles(args.prefix))
    fd = os.open(args.secrets_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(pw, f, indent=2)
    print(f"created roles {', '.join(pw)} and database {args.db}; passwords in {args.secrets_file}")


def cmd_init(args) -> None:
    from .admin import init_hive
    from .keys import valid_key
    from .store import Roles
    if not valid_key(args.operator_key):
        raise SystemExit("--operator-key must be an ed25519:<base64url> public key")
    decl = dict(harness="human", model_route="none", sandbox="none")
    for kv in args.declaration or []:
        k, _, v = kv.partition("=")
        decl[k] = v
    gen = init_hive(args.admin_url, Roles(args.prefix), hive=args.hive, profile=args.profile, mode=args.mode,
                    policy_pin=_read_pin(args.policy), registry_path=args.registry,
                    operator={"id": args.operator_id, "role": args.operator_role,
                              "keys": [args.operator_key], "declaration": decl})
    print(f"initialized hive {args.hive} (generation {gen})")


def _source_commit() -> str:
    try:
        return subprocess.run(["git", "-C", str(Path(__file__).parent), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def cmd_gateway(args) -> None:
    import logging

    import uvicorn

    from .server import Gateway, GatewayConfig
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    gw = Gateway(GatewayConfig(db_url=args.db_url, registry_path=args.registry,
                               source_commit=args.source_commit or _source_commit()))
    ev = gw.start()
    logging.getLogger("hiverecord.gateway").info("gateway.started at position %d", ev["position"])
    if args.uds:
        uvicorn.run(gw.app(), uds=args.uds, log_level="info")
    else:
        uvicorn.run(gw.app(), host=args.host, port=args.port, log_level="info")


def cmd_emit(args) -> None:
    c = _client(args)
    data = json.loads(args.data) if args.data else {}
    refs = []
    for r in args.ref or []:
        rel, _, pin = r.partition("=")
        refs.append({"rel": rel, "pin": _read_pin(pin)})
    rev = args.expected_revision
    if args.task and rev is None and not args.no_revision:
        try:
            rev = c.task(args.task)["task"]["revision"]   # SPEC §17.4: act on what was just read
        except Exception:
            rev = None                                     # e.g. task.created
    try:
        payload = c.emit(args.type, data=data, refs=refs, task=args.task, goal=args.goal,
                         expected_revision=rev, idempotency_key=args.idempotency_key)
    except Exception as exc:
        payload = getattr(exc, "payload", None)
        if payload is None:
            raise
        _out({k: v for k, v in payload.items() if k != "refusal"})
        raise SystemExit(1) from None
    _out(payload.get("events") or payload["event"])


def cmd_events(args) -> None:
    c = _client(args)
    filters = {"type": args.type, "task": args.task, "goal": args.goal}
    if args.follow:
        for ev in c.subscribe(args.after):
            if all(v is None or ev.get(k) == v for k, v in filters.items() if k != "goal"):
                print(dumps(ev).decode())
                sys.stdout.flush()
        return
    for ev in c.all_events(args.after, **filters):
        print(dumps(ev).decode())


def cmd_board(args) -> None:
    c = _client(args)
    state = c.state(args.at)
    sys.stdout.write(projections.board(state, project=args.project))


def cmd_task(args) -> None:
    _out(_client(args).task(args.id))


def cmd_goal(args) -> None:
    _out(_client(args).goal(args.id))


def cmd_inbox(args) -> None:
    items = _client(args).inbox(args.for_class)
    if args.json:
        _out(items)
        return
    for i in items:
        print(f"@{i['position']:<6} {i['kind']:<28} {i['entity']} {i['id']}  {i['title']}")


def cmd_actors(args) -> None:
    _out(_client(args).actors())


def cmd_state(args) -> None:
    _out(_client(args).state(args.at))


def cmd_health(args) -> None:
    from .client import Client
    from .keys import SigningKey
    url = os.environ.get("HIVE_URL") or "http://127.0.0.1:8470"
    _out(Client(url, os.environ.get("HIVE_ID", ""), SigningKey(b"\0" * 32)).health())


def cmd_export(args) -> None:
    out = sys.stdout.buffer
    if args.db_url:
        import psycopg

        from .store import export
        with psycopg.connect(args.db_url) as conn:
            for raw in export(conn, args.after):
                out.write(raw + b"\n")
        return
    for ev in _client(args).all_events(args.after):
        out.write(dumps(ev) + b"\n")


def cmd_fold(args) -> None:
    engine = _policy_engine_for(args.policy, args.registry)
    for raw in _read_log(args.file):
        ev = loads(raw)
        if args.at is not None and ev["position"] > args.at:
            break
        engine.apply(ev)
    if args.board:
        sys.stdout.write(projections.board(engine.state, project=args.project))
    elif args.inbox:
        _out(projections.inbox(engine.state, engine.policy, args.inbox))
    else:
        sys.stdout.buffer.write(dumps(engine.state) + b"\n")


def cmd_verify_log(args) -> None:
    from .verify import verify_log
    problems = verify_log(_read_log(args.file), _policy_engine_for(args.policy, args.registry))
    for p in problems:
        print(p)
    if problems:
        raise SystemExit(1)
    print("ok: every event is canonical and every signature re-verifies")


def cmd_scratch(args) -> None:
    from . import scratch
    if args.action == "up":
        s = scratch.up(args.name)
        _out({"name": s.name, "port": s.port, "superuser_url": s.superuser_url})
    elif args.all:
        print("\n".join(scratch.down_all()))
    else:
        scratch.down(args.name)


def cmd_restore(args) -> None:
    from .store import Roles, restore
    gen = restore(args.admin_url, Roles(args.prefix), _read_log(args.file))
    print(f"restored; new generation {gen}. Restart the gateway.")


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hive", description="The hive record (One Hive R1+R2).",
                                epilog=ENV_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"hiverecord {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("keygen", help="create an ed25519 key pair; prints only the public key")
    s.add_argument("--out", required=True, help="private key file to create (mode 0600)")
    s.set_defaults(fn=cmd_keygen)

    s = sub.add_parser("db-bootstrap", help="create roles and database (superuser)")
    s.add_argument("--superuser-url", required=True)
    s.add_argument("--db", required=True)
    s.add_argument("--prefix", default="hive", help="role name prefix (default: hive)")
    s.add_argument("--secrets-file", required=True, help="where to write the role passwords (0600)")
    s.set_defaults(fn=cmd_db_bootstrap)

    s = sub.add_parser("init", help="initialize a hive (as the admin role)")
    s.add_argument("--admin-url", required=True)
    s.add_argument("--prefix", default="hive")
    s.add_argument("--hive", required=True)
    s.add_argument("--profile", required=True)
    s.add_argument("--policy", required=True, help="policy tree pin: file, JSON or hivepin:v1 envelope")
    s.add_argument("--mode", choices=["authoritative", "mirror"], required=True)
    s.add_argument("--registry", required=True, help="hivepin repository registry file")
    s.add_argument("--operator-id", required=True)
    s.add_argument("--operator-key", required=True, help="the operator's PUBLIC key")
    s.add_argument("--operator-role", default="owner")
    s.add_argument("--declaration", action="append", metavar="K=V")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("gateway", help="run the gateway")
    s.add_argument("--db-url", required=True, help="connection URL for the gateway role")
    s.add_argument("--registry", required=True)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8470)
    s.add_argument("--uds", help="listen on a Unix socket instead")
    s.add_argument("--source-commit")
    s.set_defaults(fn=cmd_gateway)

    s = sub.add_parser("emit", help="submit a signed request")
    s.add_argument("type")
    s.add_argument("--task")
    s.add_argument("--goal")
    s.add_argument("--data", help="JSON object")
    s.add_argument("--ref", action="append", metavar="REL=PIN")
    s.add_argument("--expected-revision", type=int)
    s.add_argument("--no-revision", action="store_true", help="do not send the task's current revision")
    s.add_argument("--idempotency-key")
    s.set_defaults(fn=cmd_emit)

    s = sub.add_parser("events", help="list or follow events")
    s.add_argument("--after", type=int, default=0)
    s.add_argument("--type")
    s.add_argument("--task")
    s.add_argument("--goal")
    s.add_argument("--follow", action="store_true")
    s.set_defaults(fn=cmd_events)

    s = sub.add_parser("board", help="open tasks by state")
    s.add_argument("--project")
    s.add_argument("--at", type=int)
    s.set_defaults(fn=cmd_board)

    for name, fn in (("task", cmd_task), ("goal", cmd_goal)):
        s = sub.add_parser(name, help=f"a {name} and its history")
        s.add_argument("id")
        s.set_defaults(fn=fn)

    s = sub.add_parser("inbox", help="the inbox for a class")
    s.add_argument("--for", dest="for_class", default="operator")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_inbox)

    sub.add_parser("actors", help="the address book").set_defaults(fn=cmd_actors)
    s = sub.add_parser("state", help="the fold at a position")
    s.add_argument("--at", type=int)
    s.set_defaults(fn=cmd_state)
    sub.add_parser("health", help="gateway health (unauthenticated)").set_defaults(fn=cmd_health)

    s = sub.add_parser("export", help="canonical JSONL of the log")
    s.add_argument("--after", type=int, default=0)
    s.add_argument("--db-url", help="read the database directly (reader role) instead of the API")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("fold", help="fold an exported log without a database")
    s.add_argument("file", help="JSONL export, or - for stdin")
    s.add_argument("--policy", action="append", metavar="DIR",
                   help="a policy tree the log pins (repeatable); checked against the pin")
    s.add_argument("--registry", metavar="FILE",
                   help="hivepin registry: materialize any pinned policy tree not given with --policy")
    s.add_argument("--at", type=int)
    s.add_argument("--board", action="store_true")
    s.add_argument("--project")
    s.add_argument("--inbox", metavar="CLASS")
    s.set_defaults(fn=cmd_fold)

    s = sub.add_parser("verify-log", help="re-verify every signature in an exported log")
    s.add_argument("file")
    s.add_argument("--policy", action="append", metavar="DIR",
                   help="a policy tree the log pins (repeatable)")
    s.add_argument("--registry", metavar="FILE",
                   help="hivepin registry: materialize any pinned policy tree not given with --policy")
    s.set_defaults(fn=cmd_verify_log)

    s = sub.add_parser("scratch", help="a throwaway Postgres in rootless Podman")
    s.add_argument("action", choices=["up", "down"])
    s.add_argument("--name")
    s.add_argument("--all", action="store_true", help="down: remove every scratch container")
    s.set_defaults(fn=cmd_scratch)

    s = sub.add_parser("restore", help="replace the log with an export; new generation (admin)")
    s.add_argument("file")
    s.add_argument("--admin-url", required=True)
    s.add_argument("--prefix", default="hive")
    s.set_defaults(fn=cmd_restore)
    return p


def main(argv: list[str] | None = None) -> None:
    from .policy import PolicyError
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except PolicyError as e:
        raise SystemExit(f"hive: {e} (pass every policy tree the log pins with --policy, or --registry)") from None


if __name__ == "__main__":
    main()
