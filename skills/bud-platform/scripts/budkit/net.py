"""Shared TLS configuration for the toolkit.

Python's ``urllib`` verifies server certificates against OpenSSL's default trust
store, which on macOS (python.org / framework builds) is frequently empty - so
every HTTPS call fails with ``CERTIFICATE_VERIFY_FAILED: unable to get local
issuer certificate`` until the caller exports ``SSL_CERT_FILE``. To spare every
caller that dance, resolve a working CA bundle here, once, and reuse it.

Precedence:
  1. an explicit ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` the environment set,
  2. the ``certifi`` bundle when that package is importable,
  3. the system default (unchanged from before).
"""

from __future__ import annotations

import functools
import os
import ssl


@functools.lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """A verifying SSL context with a CA bundle that actually loads."""
    # create_default_context() already honours SSL_CERT_FILE / SSL_CERT_DIR and
    # loads the system default locations.
    ctx = ssl.create_default_context()
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return ctx
    try:
        import certifi

        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        # No certifi (or it failed to load) -> keep the system default, i.e.
        # exactly the behaviour callers had before this helper existed.
        pass
    return ctx
