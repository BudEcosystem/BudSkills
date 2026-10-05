"""HTTP client for the Bud Foundry API.

Responsibilities that every skill would otherwise reimplement:

  * attach the session cookie and, on mutations, the CSRF header
  * renew the session once and replay the request when it has expired
  * back off politely on rate limits and transient upstream errors, so a busy
    agent never becomes the reason the platform is slow
  * walk paged collections
  * unwrap Bud's response envelopes, which nest the payload under an
    entity-specific key rather than returning it at the top level
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

from .auth import Session
from .config import Config
from .errors import BudAPIError, BudAuthError
from .tokens import TokenProvider


# Retry on statuses that represent "try again", never on 4xx that represent
# "you asked for the wrong thing".
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# Keys that wrap a paged collection's items across the API.
_LIST_KEYS = (
    "items",
    "results",
    "data",
    "projects",
    "clusters",
    "models",
    "endpoints",
    "workflows",
    "prompts",
    "connections",
    "connectors",
    "triggers",
    "experiments",
    "datasets",
    "evaluations",
    "benchmarks",
    "credentials",
    "users",
    "routers",
    "guardrails",
    "adapters",
    "workers",
    "runs",
)


class BudClient:
    """Authenticated, retrying client for one Bud Foundry installation."""

    def __init__(self, config: Config | None = None, quiet: bool = True) -> None:
        self.config = config or Config()
        self.config.require("api_url")
        self.quiet = quiet
        self.session = Session(self.config.api_url, self.config.ui_url, jar_path=self.config.session_path)
        self.tokens = TokenProvider(self.config)
        self._signed_in = False

    # -- session --------------------------------------------------------

    def login(self, force: bool = False) -> None:
        self.session.ensure(self.config.email, self.config.password, force=force)
        self._signed_in = True

    def _ensure_session(self) -> None:
        if not self._signed_in:
            self.session.ensure(self.config.email, self.config.password)
            self._signed_in = True

    # -- core request ---------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 120,
        retries: int | None = None,
        auth: bool = True,
    ) -> Any:
        """Perform one API call and return the decoded body.

        Raises BudAPIError on a non-2xx response that survived retries.
        """
        method = method.upper()
        if retries is None:
            # Verification sweeps and interactive use want a smaller budget than
            # unattended automation; honour an override from the environment.
            try:
                retries = int(os.environ.get("BUD_MAX_RETRIES", "4"))
            except ValueError:
                retries = 4
        # Prefer bearer-token auth when a token source is configured (e.g. the
        # desktop app's auth.json). But if that token is expired and we also hold
        # a live cookie session from `bud login`, prefer the cookie -- a stale
        # desktop token must not shadow a fresh interactive sign-in.
        use_bearer = auth and self.tokens.available()
        if use_bearer and self.tokens.is_expired() and self.session.has_session:
            use_bearer = False
        if auth and not use_bearer:
            self._ensure_session()

        url = self._build_url(path, params)
        attempt = 0
        renewed = False

        while True:
            attempt += 1
            req = urllib.request.Request(url, method=method)
            req.add_header("Accept", "application/json")
            # Bud treats the console origin as the trusted caller; requests
            # without it are rejected as cross-site.
            req.add_header("Referer", self.config.ui_url + "/")
            req.add_header("Origin", self.config.ui_url)
            if raw_body is not None:
                req.data = raw_body
            elif json_body is not None:
                req.data = json.dumps(json_body).encode()
                req.add_header("Content-Type", "application/json")
            if use_bearer:
                # Bearer auth is validated directly against the identity provider;
                # it needs no CSRF token (that is a cookie-session construct).
                req.add_header("Authorization", f"Bearer {self.tokens.current()}")
            elif method in _MUTATING and auth:
                token = self.session.csrf_token
                if token:
                    req.add_header("x-csrf-token", token)
            for key, value in (headers or {}).items():
                req.add_header(key, value)

            try:
                with self.session._open(req, timeout=timeout) as resp:
                    body = resp.read().decode("utf-8", "replace")
                return _decode(body)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")

                # An expired session/token is worth exactly one silent renewal.
                if exc.code == 401 and auth and not renewed:
                    renewed = True
                    attempt -= 1
                    if use_bearer:
                        try:
                            if not self.tokens.handle_401():
                                raise BudAPIError(exc.code, method, path, body) from exc
                        except BudAuthError as aexc:
                            raise BudAPIError(exc.code, method, path, str(aexc)) from exc
                        continue
                    try:
                        self.session.ensure(self.config.email, self.config.password, force=True)
                    except BudAuthError:
                        raise BudAPIError(exc.code, method, path, body) from exc
                    continue

                if exc.code in _RETRY_STATUSES and attempt <= retries:
                    self._sleep_backoff(attempt, exc.headers.get("Retry-After"))
                    continue
                raise BudAPIError(exc.code, method, path, body) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt <= retries:
                    self._sleep_backoff(attempt, None)
                    continue
                raise BudAPIError(0, method, path, f"network error: {exc}") from exc

    def _build_url(self, path: str, params: dict[str, Any] | None) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = self.config.api_url + "/" + path.lstrip("/")
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                flat: list[tuple[str, str]] = []
                for key, value in clean.items():
                    if isinstance(value, (list, tuple)):
                        flat.extend((key, str(v)) for v in value)
                    elif isinstance(value, bool):
                        flat.append((key, "true" if value else "false"))
                    else:
                        flat.append((key, str(value)))
                sep = "&" if "?" in url else "?"
                url = url + sep + urllib.parse.urlencode(flat)
        return url

    def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 60.0))
                return
            except (TypeError, ValueError):
                pass
        # Exponential with jitter, capped -- keeps a retry storm from turning
        # a slow platform into an unavailable one.
        delay = min(2.0 ** (attempt - 1), 30.0) * (0.5 + random.random() / 2)
        time.sleep(delay)

    # -- verbs ----------------------------------------------------------

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, json_body: Any = None, **kw: Any) -> Any:
        return self.request("POST", path, json_body=json_body, **kw)

    def put(self, path: str, json_body: Any = None, **kw: Any) -> Any:
        return self.request("PUT", path, json_body=json_body, **kw)

    def patch(self, path: str, json_body: Any = None, **kw: Any) -> Any:
        return self.request("PATCH", path, json_body=json_body, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)

    # -- collections ----------------------------------------------------

    def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        page_size: int = 50,
        max_items: int | None = None,
        page_param: str = "page",
        size_param: str = "limit",
    ) -> Iterator[dict]:
        """Yield every item of a paged collection.

        Stops when a page comes back short or the reported total is reached, so
        it terminates even if the server ignores the page parameter.
        """
        page = 1
        seen = 0
        while True:
            query = dict(params or {})
            query[page_param] = page
            query[size_param] = page_size
            payload = self.get(path, params=query)
            items = extract_list(payload)
            if not items:
                return
            for item in items:
                yield item
                seen += 1
                if max_items is not None and seen >= max_items:
                    return
            total = payload.get("total_record") if isinstance(payload, dict) else None
            if len(items) < page_size:
                return
            if isinstance(total, int) and seen >= total:
                return
            page += 1

    def find_one(self, path: str, match: dict[str, Any], *, params: dict[str, Any] | None = None) -> dict | None:
        """First item in a collection whose fields match (case-insensitive)."""
        for item in self.paginate(path, params=params, max_items=2000):
            flat = unwrap(item)
            if all(str(flat.get(k, "")).strip().lower() == str(v).strip().lower() for k, v in match.items()):
                return flat
        return None


def _decode(body: str) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return body


def extract_list(payload: Any) -> list:
    """Find the item array in a list response, whatever it is called."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in _LIST_KEYS:
        val = payload.get(key)
        if isinstance(val, list):
            return val
    # Fall back to the only list-valued key, if there is exactly one.
    lists = [v for v in payload.values() if isinstance(v, list)]
    return lists[0] if len(lists) == 1 else []


def unwrap(payload: Any, *keys: str) -> Any:
    """Peel Bud's response envelope to reach the entity.

    Create and fetch responses nest the entity under its own name, e.g.
    ``{"object": "...", "message": "...", "project": {...}}``. List items do the
    same (``{"project": {...}, "users_count": 1}``). Given explicit keys, try
    those; otherwise take the single nested object that is not envelope
    metadata.
    """
    if not isinstance(payload, dict):
        return payload
    for key in keys:
        val = payload.get(key)
        if isinstance(val, dict):
            return val
    envelope = {"object", "message", "code", "type", "param", "page", "limit", "total_record"}
    nested = {k: v for k, v in payload.items() if isinstance(v, dict) and k not in envelope and v}
    if len(nested) == 1:
        inner = next(iter(nested.values()))
        if "id" in inner or "name" in inner:
            return inner
    return payload


def entity_id(payload: Any, *keys: str) -> str | None:
    """Best-effort id extraction from any create/fetch response."""
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in ("workflow_id", "id", "uuid"):
        val = payload.get(key)
        if isinstance(val, str):
            return val
    inner = unwrap(payload, *keys)
    if isinstance(inner, dict) and inner is not payload:
        return entity_id(inner)
    return None
