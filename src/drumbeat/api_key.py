"""API key authentication for the engine's HTTP API.

**There is no loopback bypass on mutating requests.** That is the one place
this deliberately diverges from the consumer's own service, and the reason
is the topology, not taste (docs/ARCHITECTURE.md section 2, council
amendment 6):

    A loopback bypass was the right call at N=1 service on a host. At N>=2
    co-hosted engine instances -- which is the whole point of the split,
    since the next consuming project runs its own -- a loopback bypass makes
    every engine authless to every local process. A pack binary running
    inside consumer B's turn could curl keyless turns into consumer A's
    pinned sessions. The blast radius of "local means trusted" grows with
    every engine added to the host.

So: **reads may keep the loopback convenience; writes never do.** A write
presents the key or it is refused, no matter where it came from. Whether a
request is a write is decided by HTTP method, not by path -- a path
allowlist is a thing you forget to update when you add an endpoint, and the
failure mode of forgetting is an unauthenticated mutation.

The key is per-instance and lives with the workspace it serves
(``<runs_dir>/.drumbeat-api-key``, mode 600), not in a user-global location:
two engines on one host must not share one key, or the isolation above is
theatre.

FAIL LOUD, NO FALLBACKS: there is no flag to disable this check. A
disabled-auth escape hatch is exactly the kind of thing that gets left on.
"""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path

API_KEY_FILENAME = ".drumbeat-api-key"

# Socket-level loopback addresses. Deliberately NOT derived from any header
# (X-Forwarded-For, Host, ...) -- those are attacker-controlled. The value
# passed to check_request() must come from the server socket's client
# address, which the client cannot forge.
LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1"})

# Methods that change state. Everything not in here is treated as a read.
# Note the direction of the default: an unknown/exotic method is treated as
# a WRITE (key required), never as a read. Guessing "probably safe" about a
# method we do not recognise is how an unauthenticated mutation ships.
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Paths that never require the header even for a non-loopback client, so a
# bare connectivity check can always answer "is this engine up" with no key
# in hand. Reads only, by construction -- see check_request.
PUBLIC_PATHS = frozenset({"/api/health"})


def api_key_path(runs_dir: Path) -> Path:
    """This instance's key file, beside the workspace data it protects."""
    return Path(runs_dir).expanduser() / API_KEY_FILENAME


def _generate_and_persist(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    path.write_text(key + "\n", encoding="utf-8")
    path.chmod(0o600)
    return key


def ensure_api_key(runs_dir: Path) -> str:
    """Load this instance's key, generating one on first use.

    FAIL LOUD: a file that exists but is empty raises rather than silently
    minting a replacement. An empty file means something already went wrong
    (truncated write, a bad edit), and quietly regenerating would invalidate
    every client holding the old key without telling anyone.
    """
    path = api_key_path(runs_dir)
    if path.is_file():
        key = path.read_text(encoding="utf-8").strip()
        if not key:
            raise RuntimeError(
                f"engine API key file exists but is empty: {path} -- refusing "
                "to silently regenerate. Delete the file to mint a new key "
                "deliberately (every client holding the old one must be updated)."
            )
        return key
    return _generate_and_persist(path)


def is_loopback(client_address: str) -> bool:
    return client_address in LOOPBACK_ADDRESSES


def is_mutating(method: str) -> bool:
    """True when this HTTP method changes state (and so always needs the key)."""
    return method.upper() not in READ_METHODS


def check_request(
    *,
    client_address: str,
    method: str,
    path: str,
    header_value: str | None,
    expected_key: str,
) -> str | None:
    """Return ``None`` if the request is authorized, else an error message.

    Authorized when EITHER:

    - the ``X-API-Key`` header matches, compared with ``hmac.compare_digest``
      so the comparison cannot be timed; or
    - the request is a **read** (see ``READ_METHODS``) AND either the client
      is socket-level loopback or the path is public.

    A mutating request is authorized by the key and nothing else. Loopback
    is not a credential for writes here -- see the module docstring.
    """
    mutating = is_mutating(method)

    if not mutating and (is_loopback(client_address) or path in PUBLIC_PATHS):
        return None

    if not header_value:
        if mutating:
            return (
                "missing required header: X-API-Key (required on every mutating "
                "request, including from loopback -- see docs/ARCHITECTURE.md section 2)"
            )
        return "missing required header: X-API-Key"
    if not hmac.compare_digest(header_value, expected_key):
        return "incorrect X-API-Key header"
    return None


__all__ = [
    "API_KEY_FILENAME",
    "LOOPBACK_ADDRESSES",
    "PUBLIC_PATHS",
    "READ_METHODS",
    "api_key_path",
    "check_request",
    "ensure_api_key",
    "is_loopback",
    "is_mutating",
]
