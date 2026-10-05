"""``bud`` -- command line access to a Bud Foundry installation.

Design notes for anyone extending this:

* Every command prints JSON on stdout and progress/diagnostics on stderr, so
  output can be piped into ``jq`` or parsed by an agent without stripping
  chatter.
* ``bud api`` is a deliberate escape hatch: any endpoint, including ones no
  skill documents, is reachable through it with auth, CSRF and retries handled.
* ``bud paths`` / ``bud schema`` let a caller discover the API at runtime
  instead of relying on documentation that can drift.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .client import BudClient, extract_list, unwrap
from .config import Config, save_config
from .errors import BudError
from .waits import (
    fetch_job,
    present,
    stderr_progress,
    summarize,
    wait_resource,
    wait_workflow,
    workflow_steps,
)


def _out(obj: Any) -> None:
    if isinstance(obj, (dict, list)):
        json.dump(obj, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(f"{obj}\n")


def _err(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")


def _client(args: argparse.Namespace) -> BudClient:
    cfg = Config(
        api_url=getattr(args, "api_url", None),
        ui_url=getattr(args, "ui_url", None),
        profile=getattr(args, "profile", None),
    )
    return BudClient(cfg)


def _parse_body(raw: str | None) -> Any:
    """Accept inline JSON, @file, or @- for stdin."""
    if raw is None:
        return None
    if raw.startswith("@"):
        source = raw[1:]
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    else:
        text = raw
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise SystemExit(f"bud: request body is not valid JSON: {exc}")


def _parse_kv(pairs: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"bud: expected key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# OpenAPI discovery
# ---------------------------------------------------------------------------


def _spec_path(cfg: Config) -> Path:
    from .config import DEFAULT_CONFIG_DIR

    host = (cfg.api_url or "unknown").replace("https://", "").replace("/", "_")
    return DEFAULT_CONFIG_DIR / f"openapi-{host}.json"


def _load_spec(client: BudClient, refresh: bool = False) -> dict:
    """Fetch and cache the installation's own API description."""
    path = _spec_path(client.config)
    if not refresh and path.exists() and time.time() - path.stat().st_mtime < 86400:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    spec = client.get("/openapi.json", auth=False, timeout=90)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec), encoding="utf-8")
    return spec


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_login(args: argparse.Namespace) -> int:
    cfg = Config(
        api_url=args.api_url,
        ui_url=args.ui_url,
        email=args.email,
        password=args.password,
        profile=args.profile,
    )
    cfg.require("api_url")

    # Bearer path: OIDC-only installs have no password login. If a token source
    # is present (env, token file, or the desktop app's auth.json), validate it
    # instead of asking for a password.
    client = BudClient(cfg)
    if client.tokens.available():
        me = client.get("/users/me")
        save_config({"api_url": cfg.api_url, "ui_url": cfg.ui_url})
        _err(f"Signed in to {cfg.api_url} with a bearer token (source: {client.tokens.source()}).")
        _out(unwrap(me, "user"))
        return 0

    if not cfg.email:
        raise SystemExit(
            "bud: no credentials. Set BUD_ACCESS_TOKEN (or sign in via the Bud Studio "
            "desktop app so it writes auth.json) for OIDC-only installs, or pass --email / "
            "set BUD_EMAIL for password sign-in."
        )
    if not cfg.password:
        pw = os.environ.get("BUD_PASSWORD")
        if not pw:
            import getpass

            pw = getpass.getpass(f"Password for {cfg.email}: ")
        cfg._values["password"] = pw
    client.login(force=args.force)
    me = client.get("/users/me")
    save_config({"api_url": cfg.api_url, "ui_url": cfg.ui_url, "email": cfg.email})
    _err(f"Signed in to {cfg.api_url}")
    _out(unwrap(me, "user"))
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    client = _client(args)
    _out(unwrap(client.get("/users/me"), "user"))
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    client = _client(args)
    client.session.clear()
    client.tokens.clear()
    _err("Signed out (local session and cached token discarded).")
    return 0


def cmd_configure(args: argparse.Namespace) -> int:
    values = {k: v for k, v in vars(args).items() if k in ("api_url", "ui_url", "email") and v}
    path = save_config(values)
    _err(f"Wrote {path}")
    _out(Config().as_dict())
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    client = _client(args)
    body = _parse_body(args.data)
    params = _parse_kv(args.query)
    if args.paginate:
        items = list(
            client.paginate(
                args.path,
                params=params,
                max_items=args.max_items,
                page_size=args.page_size,
            )
        )
        _out(items)
        return 0
    result = client.request(args.method, args.path, params=params, json_body=body, timeout=args.timeout)
    _out(result)
    return 0


def cmd_paths(args: argparse.Namespace) -> int:
    client = _client(args)
    spec = _load_spec(client, refresh=args.refresh)
    needles = [n.lower() for n in args.keyword]
    rows = []
    for path, ops in sorted(spec.get("paths", {}).items()):
        for method, op in ops.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            summary = op.get("summary", "") or ""
            hay = f"{path} {method} {summary} {op.get('description', '')}".lower()
            if all(n in hay for n in needles):
                rows.append({"method": method.upper(), "path": path, "summary": summary})
    if args.json:
        _out(rows)
    else:
        for r in rows:
            print(f"{r['method']:6} {r['path']:60} {r['summary']}")
        sys.stdout.flush()
        _err(f"\n{len(rows)} endpoint(s) matched.")
    return 0


def _resolve_ref(spec: dict, ref: str) -> dict:
    node: Any = spec
    for part in ref.lstrip("#/").split("/"):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, dict) else {}


def _flatten_schema(spec: dict, schema: dict, depth: int = 0) -> Any:
    """Render a JSON schema as a compact example-ish structure."""
    if depth > 6 or not isinstance(schema, dict):
        return "..."
    if "$ref" in schema:
        return _flatten_schema(spec, _resolve_ref(spec, schema["$ref"]), depth + 1)
    for key in ("allOf", "anyOf", "oneOf"):
        if key in schema and schema[key]:
            options = [o for o in schema[key] if isinstance(o, dict)]
            non_null = [o for o in options if o.get("type") != "null"] or options
            return _flatten_schema(spec, non_null[0], depth + 1)
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        required = set(schema.get("required", []))
        return {
            f"{name}{'' if name in required else ' (optional)'}": _flatten_schema(spec, sub, depth + 1)
            for name, sub in (schema.get("properties") or {}).items()
        }
    if kind == "array":
        return [_flatten_schema(spec, schema.get("items", {}), depth + 1)]
    if schema.get("enum"):
        return " | ".join(str(e) for e in schema["enum"])
    hint = kind or "any"
    if schema.get("format"):
        hint += f"({schema['format']})"
    if schema.get("description"):
        hint += f" -- {schema['description'][:90]}"
    return hint


def cmd_schema(args: argparse.Namespace) -> int:
    client = _client(args)
    spec = _load_spec(client, refresh=args.refresh)
    paths = spec.get("paths", {})
    target = args.path
    if target not in paths:
        matches = [p for p in paths if target in p]
        if len(matches) == 1:
            target = matches[0]
        elif matches:
            _err("Multiple paths match; be more specific:")
            for m in matches[:25]:
                _err(f"  {m}")
            return 2
        else:
            _err(f"No such path: {args.path}. Try: bud paths {args.path}")
            return 2
    ops = paths[target]
    methods = [args.method.lower()] if args.method else list(ops)
    out: dict[str, Any] = {"path": target}
    for method in methods:
        op = ops.get(method)
        if not isinstance(op, dict):
            continue
        entry: dict[str, Any] = {"summary": op.get("summary", "")}
        params = [
            {
                "name": p.get("name"),
                "in": p.get("in"),
                "required": p.get("required", False),
                "type": _flatten_schema(spec, p.get("schema", {})),
            }
            for p in op.get("parameters", [])
            if isinstance(p, dict)
        ]
        if params:
            entry["parameters"] = params
        body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
        if body:
            entry["request_body"] = _flatten_schema(spec, body)
        ok = op.get("responses", {}).get("200") or op.get("responses", {}).get("201")
        if isinstance(ok, dict):
            rs = ok.get("content", {}).get("application/json", {}).get("schema")
            if rs:
                entry["response"] = _flatten_schema(spec, rs)
        out[method.upper()] = entry
    _out(out)
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    client = _client(args)
    payload = client.get("/workflows", params={"page": 1, "limit": args.limit})
    rows = []
    for item in extract_list(payload):
        rows.append(
            {
                "id": item.get("id"),
                "kind": item.get("workflow_type"),
                "title": present(item.get("title")),
                "status": item.get("status"),
                "step": f"{item.get('current_step')}/{item.get('total_steps')}",
                "created_at": item.get("created_at"),
            }
        )
    _out(rows)
    return 0


def cmd_job_show(args: argparse.Namespace) -> int:
    client = _client(args)
    wf = fetch_job(client, args.job_id)
    out: dict[str, Any] = {
        "id": wf.get("id"),
        "kind": wf.get("kind"),
        "status": wf.get("status"),
        "summary": summarize(wf),
        "reason": present(wf.get("reason")),
        "steps": [
            {k: present(v) if k in ("title", "message") else v for k, v in s.items()} for s in workflow_steps(wf)
        ],
    }
    if args.data:
        out["data"] = wf.get("data")
    _out(out)
    return 0


def cmd_wait_job(args: argparse.Namespace) -> int:
    client = _client(args)
    wf = wait_workflow(
        client,
        args.job_id,
        timeout=args.timeout,
        interval=args.interval,
        on_update=stderr_progress("  "),
    )
    _err("Job settled.")
    _out({"id": wf.get("id"), "status": wf.get("status"), "summary": summarize(wf)})
    return 0


def cmd_wait_resource(args: argparse.Namespace) -> int:
    client = _client(args)
    record = wait_resource(
        client,
        args.path,
        params=_parse_kv(getattr(args, "query", None)),
        field=args.field,
        equals={v.strip() for v in args.equals.split(",")} if args.equals else None,
        fail_on={v.strip() for v in args.fail_on.split(",")} if args.fail_on else None,
        timeout=args.timeout,
        interval=args.interval,
        on_update=stderr_progress("  "),
    )
    _err("Reached the desired state.")
    _out(record)
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    """Mint a bearer token accepted by the inference gateway.

    Two calls are needed and the second is easy to miss: the gateway does not
    validate tokens itself, it looks them up in a registry. A token that was
    never registered is rejected as an invalid key, and a token refreshed since
    the last registration is rejected too.
    """
    client = _client(args)
    expires = None
    if client.tokens.available():
        # Bearer mode: the access token is already a provider JWT; ws-token needs
        # the cookie session we don't have, so register the bearer token directly.
        token = client.tokens.current()
    else:
        payload = client.get("/auth/redirect/ws-token")
        token = payload.get("ws_token") if isinstance(payload, dict) else None
        expires = payload.get("expires_at") if isinstance(payload, dict) else None
    if not token:
        _err("Could not obtain a token for this session. Try: bud login --force")
        return 1
    if not args.no_register:
        client.post("/playground/initialize-with-token", {"access_token": token})
    if args.export:
        print(f"export BUD_GATEWAY_TOKEN={token}")
    else:
        print(token)
    if expires:
        _err(f"Token registered with the inference gateway; expires at epoch {expires}.")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    client = _client(args)
    report: dict[str, Any] = {}
    try:
        report["api"] = client.get("/health", auth=False, retries=1)
    except BudError as exc:
        report["api"] = f"unreachable: {exc}"
    for label, path in (
        ("identity", "/users/me"),
        ("projects", "/projects/?page=1&limit=1"),
    ):
        try:
            client.get(path)
            report[label] = "ok"
        except BudError as exc:
            report[label] = f"error: {exc}"
    _out(report)
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bud", description="Control a Bud Foundry installation.")
    p.add_argument("--api-url", help="Bud API origin (env BUD_API_URL)")
    p.add_argument("--ui-url", help="Bud console origin (env BUD_UI_URL)")
    p.add_argument("--profile", help="named profile (env BUD_PROFILE)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="sign in and cache the session")
    s.add_argument("--email")
    s.add_argument("--password", help="prefer the BUD_PASSWORD env var")
    s.add_argument("--force", action="store_true", help="ignore any cached session")
    s.set_defaults(func=cmd_login)

    sub.add_parser("whoami", help="show the signed-in user").set_defaults(func=cmd_whoami)
    sub.add_parser("logout", help="discard the cached session").set_defaults(func=cmd_logout)

    s = sub.add_parser("configure", help="save non-secret settings")
    s.add_argument("--email")
    s.set_defaults(func=cmd_configure)

    s = sub.add_parser("api", help="call any endpoint (auth, CSRF and retries handled)")
    s.add_argument("method", help="GET/POST/PUT/PATCH/DELETE")
    s.add_argument("path", help="e.g. /projects/")
    s.add_argument("-d", "--data", help="JSON body, @file, or @- for stdin")
    s.add_argument("-q", "--query", action="append", help="query param key=value (repeatable)")
    s.add_argument("--paginate", action="store_true", help="walk every page and emit one array")
    s.add_argument("--page-size", type=int, default=50)
    s.add_argument("--max-items", type=int, default=None)
    s.add_argument("--timeout", type=int, default=120)
    s.set_defaults(func=cmd_api)

    s = sub.add_parser("paths", help="search this installation's endpoints")
    s.add_argument("keyword", nargs="+", help="all keywords must match")
    s.add_argument("--json", action="store_true")
    s.add_argument("--refresh", action="store_true", help="re-download the API description")
    s.set_defaults(func=cmd_paths)

    s = sub.add_parser("schema", help="show request/response shape for an endpoint")
    s.add_argument("path")
    s.add_argument("method", nargs="?")
    s.add_argument("--refresh", action="store_true")
    s.set_defaults(func=cmd_schema)

    s = sub.add_parser("jobs", help="list recent long-running jobs")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_jobs)

    s = sub.add_parser("job", help="inspect one job")
    jsub = s.add_subparsers(dest="jobcmd", required=True)
    js = jsub.add_parser("show")
    js.add_argument("job_id")
    js.add_argument("--data", action="store_true", help="include the data collected so far")
    js.set_defaults(func=cmd_job_show)

    s = sub.add_parser("wait", help="block until something finishes")
    wsub = s.add_subparsers(dest="waitcmd", required=True)
    wj = wsub.add_parser("job", help="follow a job until its work settles")
    wj.add_argument("job_id")
    wj.add_argument("--timeout", type=int, default=3600)
    wj.add_argument("--interval", type=int, default=10)
    wj.set_defaults(func=cmd_wait_job)
    wr = wsub.add_parser("resource", help="poll a resource until a field reaches a value")
    wr.add_argument("path")
    wr.add_argument("--field", default="status")
    wr.add_argument("--equals", help="comma-separated acceptable values")
    wr.add_argument("--fail-on", help="comma-separated failure values")
    wr.add_argument("-q", "--query", action="append", help="query param key=value (repeatable)")
    wr.add_argument("--timeout", type=int, default=3600)
    wr.add_argument("--interval", type=int, default=10)
    wr.set_defaults(func=cmd_wait_resource)

    s = sub.add_parser("token", help="mint a bearer token for the inference gateway")
    s.add_argument("--export", action="store_true", help="print as a shell export statement")
    s.add_argument("--no-register", action="store_true", help="skip gateway registration (token will NOT work for inference)")
    s.set_defaults(func=cmd_token)

    sub.add_parser("health", help="check the installation is reachable and healthy").set_defaults(func=cmd_health)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BudError as exc:
        _err(f"bud: {exc}")
        return 1
    except KeyboardInterrupt:
        _err("bud: interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
