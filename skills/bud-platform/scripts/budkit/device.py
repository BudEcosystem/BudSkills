"""OIDC Device Authorization Grant (RFC 8628) login for the bud CLI.

OIDC-only Bud installs disable password login and register only *confidential*
Keycloak clients, so a headless toolkit cannot run a browser redirect, cannot
hold a client secret, and (on this platform) has no `auth.json` of its own to
read until the desktop app writes one. The device grant is the standard fix: a
dedicated **public** client with the device flow enabled lets the CLI
authenticate by showing the user a short code to type into their browser, then
polling for the token. No secret, no local redirect listener, no password
through the toolkit or the model.

Two steps, so a chat agent can show the code between them:

  1. ``start()``  — ask the IdP for a device + user code; persist the handle.
  2. ``wait()``   — poll the token endpoint until the user approves (or it
                    expires), then hand back the token set to cache.

Resolution:
  - **issuer**  : ``BUD_OIDC_ISSUER`` → else the ``iss`` claim of any token the
    toolkit can already see (the stale desktop ``auth.json`` works fine).
  - **client id**: ``BUD_OIDC_CLIENT_ID`` → else ``bud-cli`` (the public client
    the infra realm config registers for exactly this purpose).

Prerequisite: the ``bud-cli`` public client must exist in the realm with
``deviceAuthorizationGrantEnabled: true`` — see the infra Keycloak values.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .errors import BudAuthError
from .tokens import decode_jwt_claims

_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) BudFoundrySkill/1.0"
DEFAULT_CLIENT_ID = "bud-cli"
SCOPE = "openid profile email offline_access"


def _post_form(url: str, fields: dict[str, str], timeout: int = 30) -> tuple[int, Any]:
    """POST application/x-www-form-urlencoded; return (status, decoded-json-or-text)."""
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", _USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BudAuthError(f"Could not reach the identity provider: {exc}") from exc
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def _discover(issuer: str) -> dict[str, str]:
    """Fetch the realm's device + token endpoints from OIDC discovery."""
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", _USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            doc = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
        raise BudAuthError(f"OIDC discovery failed for {issuer}: {exc}") from exc
    dev = doc.get("device_authorization_endpoint")
    tok = doc.get("token_endpoint")
    if not dev or not tok:
        raise BudAuthError(
            f"The identity provider at {issuer} does not advertise a device "
            "authorization endpoint. A public client with the device grant must "
            "be enabled (client id 'bud-cli')."
        )
    return {"device_authorization_endpoint": dev, "token_endpoint": tok}


def resolve_issuer(config: Any, issuer_hint: str | None) -> str:
    """Issuer from explicit config, else the `iss` of a token we already hold."""
    explicit = config.get("oidc_issuer")
    if explicit:
        return str(explicit).rstrip("/")
    if issuer_hint:
        claims = decode_jwt_claims(issuer_hint)
        iss = claims.get("iss") if claims else None
        if isinstance(iss, str) and iss:
            return iss.rstrip("/")
    raise BudAuthError(
        "Cannot determine the identity provider. Set BUD_OIDC_ISSUER (e.g. "
        "https://auth.example.com/realms/<realm>) and retry."
    )


def resolve_client_id(config: Any) -> str:
    return str(config.get("oidc_client_id") or DEFAULT_CLIENT_ID)


def start(issuer: str, client_id: str) -> dict[str, Any]:
    """Begin a device authorization. Returns the IdP's device response plus the
    resolved token endpoint, ready to persist as a handle."""
    endpoints = _discover(issuer)
    status, data = _post_form(
        endpoints["device_authorization_endpoint"],
        {"client_id": client_id, "scope": SCOPE},
    )
    if status != 200 or not isinstance(data, dict) or "device_code" not in data:
        detail = data.get("error_description") if isinstance(data, dict) else data
        raise BudAuthError(f"Device authorization request was rejected (HTTP {status}): {detail}")
    expires_in = int(data.get("expires_in", 600))
    return {
        "device_code": data["device_code"],
        "user_code": data.get("user_code", ""),
        "verification_uri": data.get("verification_uri", ""),
        "verification_uri_complete": data.get("verification_uri_complete")
        or data.get("verification_uri", ""),
        "interval": int(data.get("interval", 5)),
        "expires_at": time.time() + expires_in,
        "expires_in": expires_in,
        "token_endpoint": endpoints["token_endpoint"],
        "issuer": issuer,
        "client_id": client_id,
    }


def wait(handle: dict[str, Any], sleep=time.sleep) -> dict[str, Any]:
    """Poll the token endpoint until the user approves. Returns the token set
    ({access_token, refresh_token, expires_in, ...}) or raises BudAuthError."""
    token_endpoint = handle["token_endpoint"]
    client_id = handle["client_id"]
    device_code = handle["device_code"]
    interval = max(1, int(handle.get("interval", 5)))
    expires_at = float(handle.get("expires_at", time.time() + 600))

    while True:
        if time.time() >= expires_at:
            raise BudAuthError("The sign-in code expired before it was approved. Start again.")
        status, data = _post_form(
            token_endpoint,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": client_id,
            },
        )
        if status == 200 and isinstance(data, dict) and data.get("access_token"):
            return data
        error = data.get("error") if isinstance(data, dict) else None
        if error == "authorization_pending":
            sleep(interval)
            continue
        if error == "slow_down":
            interval += 5
            sleep(interval)
            continue
        if error == "expired_token":
            raise BudAuthError("The sign-in code expired before it was approved. Start again.")
        if error == "access_denied":
            raise BudAuthError("Sign-in was declined.")
        detail = data.get("error_description") if isinstance(data, dict) else data
        raise BudAuthError(f"Device sign-in failed (HTTP {status}): {detail}")


# -- handle persistence (between `--device` start and `--device --wait`) --------


def handle_path(config: Any) -> Path:
    from .config import DEFAULT_CONFIG_DIR

    return DEFAULT_CONFIG_DIR / f"device-{config.get('profile') or 'default'}.json"


def save_handle(config: Any, handle: dict[str, Any]) -> None:
    import os

    path = handle_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(handle), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def load_handle(config: Any) -> dict[str, Any] | None:
    try:
        return json.loads(handle_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear_handle(config: Any) -> None:
    try:
        handle_path(config).unlink()
    except OSError:
        pass
