"""Bearer-token authentication for OIDC-only Bud Foundry installations.

Some installations disable password sign-in entirely and permit only the browser
OIDC redirect flow. On those, the headless password handshake in ``auth.py``
cannot work - there is no form to POST a password into. But the management API
*also* accepts a bearer token (``Authorization: Bearer <jwt>``) on every
endpoint, so a non-browser client can authenticate by reusing a token that a
real browser login already minted - for example the one the Bud Studio desktop
app stores in its ``auth.json``.

This module resolves such a token, keeps it fresh with the refresh-token grant,
and persists the rotated pair so later invocations stay signed in. It is
deliberately separate from the cookie-based ``Session``: when any bearer source
is present the client uses it instead of the password flow.

Token sources, most-trusted first by *freshness*: whichever candidate has the
latest expiry wins, so if the desktop app refreshes its ``auth.json`` in the
background we pick that up automatically rather than clinging to a stale cache.

  1. ``BUD_ACCESS_TOKEN`` (+ optional ``BUD_REFRESH_TOKEN``) in the environment
  2. a JSON token file named by ``BUD_TOKEN_FILE``
  3. the desktop app's ``auth.json`` (auto-detected per-OS, or ``BUD_DESKTOP_AUTH_FILE``)
  4. our own cache of a previously refreshed pair (``~/.bud/token-<profile>.json``)
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .errors import BudAuthError
from .net import ssl_context


# Refresh a little before the token actually expires so an in-flight request
# does not race the expiry boundary.
_EXPIRY_SKEW_SECONDS = 30

# A token whose lifetime we cannot determine is treated as valid for this long,
# which keeps it usable while still forcing a re-resolve/refresh soon after.
_UNKNOWN_TTL_SECONDS = 300

_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) BudFoundrySkill/1.0"


def decode_jwt_claims(token: str) -> dict | None:
    """Return a JWT's payload claims without verifying the signature, or None."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # pad to a multiple of 4
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _decode_jwt_exp(token: str) -> float | None:
    """Return a JWT's ``exp`` (epoch seconds) without verifying the signature."""
    claims = decode_jwt_claims(token)
    exp = claims.get("exp") if claims else None
    try:
        return float(exp) if exp is not None else None
    except (TypeError, ValueError):
        return None


def _desktop_auth_paths() -> list[Path]:
    """Candidate locations of the Bud Studio desktop app's token store."""
    override = os.environ.get("BUD_DESKTOP_AUTH_FILE")
    if override:
        return [Path(override)]

    home = Path.home()
    bundle = "com.bud.studio"
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / bundle / "auth.json"]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        return [Path(appdata) / bundle / "auth.json"] if appdata else []
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else home / ".config"
    return [base / bundle / "auth.json"]


def _normalize_expiry(raw: Any, access: str | None) -> float | None:
    """Coerce an expiry (epoch-seconds or epoch-millis) to epoch-seconds.

    Falls back to the access token's own ``exp`` claim when no expiry is given.
    """
    if isinstance(raw, (int, float)) and raw > 0:
        # Heuristic: values past year ~2286 in seconds are really milliseconds.
        return float(raw) / 1000.0 if raw > 1e11 else float(raw)
    if access:
        return _decode_jwt_exp(access)
    return None


class _Candidate:
    __slots__ = ("access", "refresh", "expires_at", "source")

    def __init__(self, access: str, refresh: str | None, expires_at: float | None, source: str) -> None:
        self.access = access
        self.refresh = refresh
        self.expires_at = expires_at
        self.source = source

    def effective_expiry(self) -> float:
        return self.expires_at if self.expires_at is not None else time.time() + _UNKNOWN_TTL_SECONDS

    def is_fresh(self) -> bool:
        return self.effective_expiry() - _EXPIRY_SKEW_SECONDS > time.time()


def _tokens_from_mapping(data: dict[str, Any], source: str) -> _Candidate | None:
    """Build a candidate from a parsed token file / refresh response."""
    access = data.get("accessToken") or data.get("access_token") or data.get("token")
    if isinstance(access, dict):  # some envelopes nest the token set under "token"
        return _tokens_from_mapping(access, source)
    if access is None:
        # The desktop app's auth.json wraps the token set under a "session" key
        # (tauri-plugin-store); other envelopes use "data". Unwrap and recurse.
        for wrapper in ("session", "data"):
            inner = data.get(wrapper)
            if isinstance(inner, dict):
                return _tokens_from_mapping(inner, source)
    if not isinstance(access, str) or not access:
        return None
    refresh = data.get("refreshToken") or data.get("refresh_token")
    expires_at = data.get("expiresAt") or data.get("expires_at")
    if expires_at is None and data.get("expires_in") is not None:
        try:
            expires_at = time.time() + float(data["expires_in"])
        except (TypeError, ValueError):
            expires_at = None
    return _Candidate(access, refresh if isinstance(refresh, str) else None, _normalize_expiry(expires_at, access), source)


def _read_token_file(path: Path, source: str) -> _Candidate | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        # A bare token string is also acceptable.
        return _Candidate(text, None, _decode_jwt_exp(text), source)
    if isinstance(data, dict):
        return _tokens_from_mapping(data, source)
    return None


class TokenProvider:
    """Resolves and refreshes a bearer token for the management API."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.api_url = (config.api_url or "").rstrip("/")
        self.ui_url = (config.ui_url or "").rstrip("/")
        self.state_path: Path = config.token_state_path
        self._current: _Candidate | None = None

    # -- discovery ------------------------------------------------------

    def _candidates(self) -> list[_Candidate]:
        out: list[_Candidate] = []

        env_access = os.environ.get("BUD_ACCESS_TOKEN") or self.config.get("access_token")
        if env_access:
            env_refresh = os.environ.get("BUD_REFRESH_TOKEN") or self.config.get("refresh_token")
            out.append(_Candidate(env_access, env_refresh, _decode_jwt_exp(env_access), "env"))

        token_file = os.environ.get("BUD_TOKEN_FILE") or self.config.get("token_file")
        if token_file:
            cand = _read_token_file(Path(token_file), f"file:{token_file}")
            if cand:
                out.append(cand)

        for path in _desktop_auth_paths():
            if path.exists():
                cand = _read_token_file(path, "desktop-app")
                if cand:
                    out.append(cand)

        if self.state_path.exists():
            cand = _read_token_file(self.state_path, "cache")
            if cand:
                out.append(cand)

        return out

    def _best(self) -> _Candidate | None:
        """The candidate with the latest expiry (freshest), if any.

        On a tie, our own refreshed cache wins - it is the most recently minted
        pair, so a reactive refresh is not undone by an equally-dated env token.
        """
        candidates = self._candidates()
        if not candidates:
            return None
        rank = {"cache": 1}  # everything else ranks 0
        return max(candidates, key=lambda c: (c.effective_expiry(), rank.get(c.source, 0)))

    def available(self) -> bool:
        """True if any bearer source is present."""
        return self._best() is not None

    def is_expired(self) -> bool:
        """True when the freshest bearer token is present but past its lifetime.
        Lets the client prefer a fresh cookie session (from `bud login`) over a
        dead desktop token."""
        best = self._best()
        return best is not None and not best.is_fresh()

    def source(self) -> str | None:
        return self._current.source if self._current else (self._best().source if self._best() else None)

    # -- the token ------------------------------------------------------

    def current(self) -> str:
        """Return a usable access token, refreshing if the freshest is stale."""
        best = self._best()
        if best is None:
            raise BudAuthError(
                "No bearer token available. Set BUD_ACCESS_TOKEN, point BUD_TOKEN_FILE at a "
                "token file, or sign in through the Bud Studio desktop app so it writes auth.json."
            )
        if best.is_fresh():
            self._current = best
            return best.access
        refreshed = self._try_refresh(best)
        if refreshed is not None:
            self._current = refreshed
            return refreshed.access
        # Stale and unrefreshable: hand back what we have and let the server's
        # 401 produce a clear, single error rather than guessing here.
        self._current = best
        return best.access

    def handle_401(self) -> bool:
        """Called after a 401: force a refresh and report whether to retry."""
        best = self._best()
        if best is None:
            return False
        refreshed = self._try_refresh(best, force=True)
        if refreshed is None:
            raise BudAuthError(
                "Bearer token rejected and could not be refreshed"
                + (f" (source: {best.source})." if best.source else ".")
                + " The session has fully expired - sign in again through the Bud Studio "
                "desktop app (it will refresh auth.json), then retry."
            )
        self._current = refreshed
        return True

    # -- refresh --------------------------------------------------------

    def _try_refresh(self, cand: _Candidate, force: bool = False) -> _Candidate | None:
        if not cand.refresh:
            return None
        try:
            new = self._refresh_grant(cand.refresh)
        except BudAuthError:
            return None
        if new is not None:
            self._persist(new)
        return new

    def _refresh_grant(self, refresh_token: str) -> _Candidate | None:
        """Exchange a refresh token for a new pair via POST /auth/refresh-token."""
        url = f"{self.api_url}/auth/refresh-token"
        body = json.dumps({"refresh_token": refresh_token}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", _USER_AGENT)
        if self.ui_url:
            req.add_header("Referer", self.ui_url + "/")
            req.add_header("Origin", self.ui_url)
        try:
            with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            # 401 "Token Expired or Invalid" lands here when the refresh token is dead.
            raise BudAuthError(f"Refresh failed (HTTP {exc.code}).") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise BudAuthError(f"Refresh request failed: {exc}") from exc
        if isinstance(data, dict):
            return _tokens_from_mapping(data, "refresh")
        return None

    def _persist(self, cand: _Candidate) -> None:
        payload = {
            "accessToken": cand.access,
            "refreshToken": cand.refresh,
            "expiresAt": int((cand.expires_at or time.time()) * 1000),
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.state_path)
        except OSError:
            pass  # a non-persistent cache is a degraded mode, not a failure

    def clear(self) -> None:
        try:
            if self.state_path.exists():
                self.state_path.unlink()
        except OSError:
            pass
