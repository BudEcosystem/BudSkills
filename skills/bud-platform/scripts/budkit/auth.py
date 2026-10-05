"""Headless sign-in for Bud Foundry.

Bud Foundry authenticates browsers with an OpenID Connect redirect flow
(Authorization Code + PKCE). There is no username/password API endpoint, so a
non-browser client must walk the same redirect handshake. This module does that
with nothing but the Python standard library -- no browser, no extra packages.

The handshake:

  1. GET  {api}/auth/redirect/authorize?return_url=/dashboard
        -> sets a short-lived PKCE cookie, 302s to the identity provider.
     The ``Referer`` header decides which console the session is minted for, so
     it must be sent. ``return_url`` must be a *path*, never an absolute URL.
  2. The identity provider's sign-in page renders its form client-side, but the
     page embeds the form's POST target. We extract it.
  3. POST the credentials to that target, following redirects back to
        {api}/auth/redirect/callback?code=..&state=..
     which mints the session and lands on the console.
  4. The cookie jar now holds the session cookie plus a CSRF cookie that every
     mutating request must echo back in a header.

A rejected password does NOT come back as an HTTP error -- the provider
re-renders the sign-in page with HTTP 200 and the reason embedded in the page.
We parse that out so callers get a real error instead of a silent failure.
"""

from __future__ import annotations

import html
import http.cookiejar
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

from .errors import BudAuthError


# The session cookie Bud sets once sign-in completes, and the CSRF companion
# that mutating requests must echo. Host-prefixed per the cookie spec.
SESSION_COOKIE = "__Host-bud_session"
CSRF_COOKIE = "__Host-bud_csrf"

# Bud's console is a browser app; the sign-in page is served only to
# browser-like clients, so we present as one.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) BudFoundrySkill/1.0"

_LOGIN_ACTION_RE = re.compile(r'"loginAction"\s*:\s*"([^"]+)"')
_FORM_ACTION_RE = re.compile(r'<form[^>]+action="([^"]+)"', re.IGNORECASE)
_MESSAGE_RE = re.compile(r'"summary"\s*:\s*"([^"]+)"')


def _unescape_action(raw: str) -> str:
    """Turn an embedded, escaped URL into a usable one."""
    return html.unescape(raw).replace("\\/", "/").replace("\\u003d", "=").replace("\\u0026", "&")


class Session:
    """A signed-in Bud Foundry session backed by a persistent cookie jar."""

    def __init__(self, api_url: str, ui_url: str, jar_path: Path | None = None) -> None:
        self.api_url = api_url.rstrip("/")
        self.ui_url = ui_url.rstrip("/")
        self.jar_path = jar_path
        self.jar = http.cookiejar.LWPCookieJar(str(jar_path) if jar_path else None)
        if jar_path and jar_path.exists():
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except (OSError, http.cookiejar.LoadError):
                pass
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPRedirectHandler(),
        )

    # -- cookie helpers -------------------------------------------------

    def cookie(self, name: str) -> str | None:
        for c in self.jar:
            if c.name == name:
                return c.value
        return None

    @property
    def csrf_token(self) -> str | None:
        return self.cookie(CSRF_COOKIE)

    @property
    def has_session(self) -> bool:
        return self.cookie(SESSION_COOKIE) is not None

    def save(self) -> None:
        if not self.jar_path:
            return
        self.jar_path.parent.mkdir(parents=True, exist_ok=True)
        self.jar.save(ignore_discard=True, ignore_expires=True)
        try:
            os.chmod(self.jar_path, 0o600)
        except OSError:
            pass

    def clear(self) -> None:
        self.jar.clear()
        if self.jar_path and self.jar_path.exists():
            try:
                self.jar_path.unlink()
            except OSError:
                pass

    # -- the handshake --------------------------------------------------

    def _open(self, req: urllib.request.Request, timeout: int = 45):
        req.add_header("User-Agent", USER_AGENT)
        return self.opener.open(req, timeout=timeout)

    def sign_in(self, email: str, password: str) -> None:
        """Complete the redirect handshake and populate the cookie jar."""
        if not email or not password:
            raise BudAuthError(
                "No credentials available. Set BUD_EMAIL and BUD_PASSWORD, or run: bud login --email you@example.com"
            )

        # Step 1 + 2: kick off the flow and land on the sign-in page.
        authorize = f"{self.api_url}/auth/redirect/authorize?" + urllib.parse.urlencode({"return_url": "/dashboard"})
        req = urllib.request.Request(authorize)
        req.add_header("Referer", self.ui_url + "/")
        try:
            resp = self._open(req)
            page = resp.read().decode("utf-8", "replace")
            landed = resp.geturl()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            raise BudAuthError(
                f"Could not start sign-in ({exc.code}). {body}\n"
                f"Check that BUD_API_URL is the Bud API origin (not the console)."
            ) from exc
        except urllib.error.URLError as exc:
            raise BudAuthError(f"Could not reach {self.api_url}: {exc.reason}") from exc

        if self.has_session:
            # Already signed in -- the provider skipped straight through.
            self.save()
            return

        match = _LOGIN_ACTION_RE.search(page) or _FORM_ACTION_RE.search(page)
        if not match:
            raise BudAuthError(
                "Reached the sign-in page but could not locate its submit target. "
                "This usually means the identity provider is presenting an "
                "unexpected screen (SSO-only, consent, or an outage).\n"
                f"Landed on: {landed}"
            )
        # Resolve relative to the page we landed on. Modern sign-in pages render
        # as a single-page app: the server ships only a shell plus a
        # ``window.kcContext = {...}`` script, so there is no server-rendered
        # ``<form>`` -- we read ``loginAction`` out of that embedded context
        # instead, and it is a **relative** path. ``urljoin`` turns it into the
        # absolute URL the POST needs; an already-absolute action is unchanged.
        action = urllib.parse.urljoin(landed, _unescape_action(match.group(1)))

        # Step 3: submit the credentials.
        form = urllib.parse.urlencode({"username": email, "password": password, "credentialId": ""}).encode()
        post = urllib.request.Request(action, data=form, method="POST")
        post.add_header("Content-Type", "application/x-www-form-urlencoded")
        post.add_header("Referer", landed)
        try:
            resp2 = self._open(post)
            body = resp2.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise BudAuthError(f"Sign-in was rejected (HTTP {exc.code}).") from exc

        # Step 4: confirm. A wrong password re-renders the page with HTTP 200.
        if not self.has_session:
            reason = _MESSAGE_RE.search(body)
            detail = reason.group(1) if reason else "no session was issued"
            raise BudAuthError(
                f"Sign-in failed: {detail}\n"
                "Repeated failures can lock the account -- verify the password "
                "before retrying rather than guessing."
            )
        self.save()

    def ensure(self, email: str | None, password: str | None, force: bool = False) -> None:
        """Reuse a cached session when it is still good, else sign in."""
        if not force and self.has_session and self.probe():
            return
        self.sign_in(email or "", password or "")

    def probe(self) -> bool:
        """Cheap liveness check of the cached session."""
        req = urllib.request.Request(f"{self.api_url}/users/me")
        req.add_header("Referer", self.ui_url + "/")
        try:
            with self._open(req, timeout=25) as resp:
                return resp.status == 200
        except urllib.error.HTTPError:
            return False
        except (urllib.error.URLError, OSError):
            # Network trouble is not proof the session died; let the caller's
            # real request surface the error rather than forcing a re-login.
            return True
