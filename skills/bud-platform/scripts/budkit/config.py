"""Configuration resolution for the Bud Foundry client.

Precedence (highest first):
  1. explicit CLI flags
  2. environment variables
  3. ~/.bud/config.json
  4. built-in defaults

No secrets are ever written to the config file by this module; credentials are
read from the environment or prompted for interactively.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_DIR = Path(os.environ.get("BUD_HOME", Path.home() / ".bud"))
CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.json"

# Keys we understand, with their environment-variable names.
_ENV = {
    "api_url": "BUD_API_URL",
    "ui_url": "BUD_UI_URL",
    "email": "BUD_EMAIL",
    "password": "BUD_PASSWORD",
    "profile": "BUD_PROFILE",
    "project_id": "BUD_PROJECT_ID",
    "inference_url": "BUD_INFERENCE_URL",
    "api_key": "BUD_API_KEY",
    "access_token": "BUD_ACCESS_TOKEN",
    "refresh_token": "BUD_REFRESH_TOKEN",
    "token_file": "BUD_TOKEN_FILE",
    "oidc_issuer": "BUD_OIDC_ISSUER",
    "oidc_client_id": "BUD_OIDC_CLIENT_ID",
}

_DEFAULTS: dict[str, Any] = {
    "api_url": None,
    "ui_url": None,
    "email": None,
    "password": None,
    "profile": "default",
    "project_id": None,
    "inference_url": None,
    "api_key": None,
    "access_token": None,
    "refresh_token": None,
    "token_file": None,
    "oidc_issuer": None,
    "oidc_client_id": None,
}

# Settings never written to the config file - secrets, or values that belong to
# the environment of a single run.
_SECRET_KEYS = ("password", "api_key", "access_token", "refresh_token")


def _read_file() -> dict[str, Any]:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    # Support both flat and profile-keyed config files.
    profile = os.environ.get("BUD_PROFILE") or raw.get("profile") or "default"
    profiles = raw.get("profiles")
    if isinstance(profiles, dict) and profile in profiles:
        merged = {k: v for k, v in raw.items() if k != "profiles"}
        merged.update(profiles[profile])
        return merged
    return raw


class Config:
    """Resolved client configuration."""

    def __init__(self, **overrides: Any) -> None:
        file_cfg = _read_file()
        self._values: dict[str, Any] = dict(_DEFAULTS)
        self._values.update({k: v for k, v in file_cfg.items() if k in _DEFAULTS})
        for key, env_name in _ENV.items():
            env_val = os.environ.get(env_name)
            if env_val:
                self._values[key] = env_val
        self._values.update({k: v for k, v in overrides.items() if v is not None})

        # A UI origin is required for the sign-in handshake; derive a sane
        # default from the API host when the caller did not supply one.
        if self._values.get("api_url") and not self._values.get("ui_url"):
            self._values["ui_url"] = _derive_ui_url(self._values["api_url"])
        for key in ("api_url", "ui_url", "inference_url"):
            if self._values.get(key):
                self._values[key] = str(self._values[key]).rstrip("/")

    def __getattr__(self, item: str) -> Any:
        try:
            return self._values[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def as_dict(self, redact: bool = True) -> dict[str, Any]:
        out = dict(self._values)
        if redact:
            for key in _SECRET_KEYS:
                if out.get(key):
                    out[key] = "***redacted***"
        return out

    @property
    def session_path(self) -> Path:
        return DEFAULT_CONFIG_DIR / f"session-{self._values['profile']}.txt"

    @property
    def token_state_path(self) -> Path:
        return DEFAULT_CONFIG_DIR / f"token-{self._values['profile']}.json"

    @property
    def state_path(self) -> Path:
        return DEFAULT_CONFIG_DIR / f"state-{self._values['profile']}.json"

    def require(self, *keys: str) -> None:
        missing = [k for k in keys if not self._values.get(k)]
        if missing:
            hints = ", ".join(f"{k} (env {_ENV.get(k, '')})" for k in missing)
            raise SystemExit(
                f"bud: missing required configuration: {hints}\nSet it via environment variable or run: bud configure"
            )


def _derive_ui_url(api_url: str) -> str:
    """Guess the console origin from the API origin.

    Bud installations conventionally pair an ``app.<domain>`` API host with an
    ``admin.<domain>`` console host. Falls back to the API origin itself, which
    is still accepted by installations that serve both from one host.
    """
    from urllib.parse import urlparse

    parsed = urlparse(api_url)
    host = parsed.netloc
    if host.startswith("app."):
        host = "admin." + host[len("app.") :]
    return f"{parsed.scheme}://{host}"


def save_config(values: dict[str, Any]) -> Path:
    """Persist non-secret configuration to disk (0600)."""
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing = _read_file()
    existing.update({k: v for k, v in values.items() if k not in _SECRET_KEYS})
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)
    return CONFIG_PATH
