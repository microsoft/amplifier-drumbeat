"""Best-effort upload of one run's captured Context Intelligence events to
the LOCAL Context Intelligence server, via the ``context-intelligence-upload``
CLI (installed separately -- see the project README for the install command).

SECURITY / DATA-RESIDENCY INVARIANT -- READ BEFORE TOUCHING THIS FILE
======================================================================
Every event this uploads carries the user's real Microsoft 365 content --
Teams messages, email subjects, meeting transcript excerpts -- read from
their employer's tenant. More than one Context Intelligence server can be
reachable from a given machine: the user's own, on this same machine
(``localhost:8100``), and potentially a colleague's, on a different host.
Sending this payload to someone else's machine would put the employer's
data on a coworker's box -- a data-residency incident, not a bug.

``_SERVER_URL`` below is therefore a hardcoded constant: never read from an
environment variable, a config file, or a CLI flag. No ambient environment
(a sibling ``CONTEXT_INTELLIGENCE_*`` variable pointing at another host, a
future misconfigured settings file, a copy-pasted wrong value) can ever
redirect this upload to the wrong machine, because there is no input path
that lets it try. If a genuine future need for a different target arises,
that must become an explicit, loudly-named parameter no ambient environment
can supply -- never a silent fallback.

FAIL LOUD, NO SILENT FALLBACKS
==============================
An upload failure must never break an automation run -- the run's actual
job (checking Teams, sending a notification) succeeded or failed on its own
merits, independent of whether the CI server happened to be reachable
afterward. But a swallowed upload error is exactly the kind of silent
failure this project has repeatedly been bitten by (see runner.py's module
docstring). Every call to ``upload_session`` returns an ``UploadOutcome``
that the caller is expected to record durably (``RunResult.ci_upload`` /
``result.json`` -- see ``runner.run()``), whether the upload succeeded,
failed, or was skipped and why.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

# Hardcoded. See module docstring -- this is the ONLY place this URL is
# ever written. NEVER read from an env var, settings file, or CLI flag.
_SERVER_URL = "http://localhost:8100"

# The API key, by contrast, legitimately comes from the environment -- it's
# a secret, not a routing decision, and using an env var for it is the
# standard convention (see the CLI's own --help). Only ONE var is ever
# consulted here; any sibling CONTEXT_INTELLIGENCE_* variable holding some
# other server's key is never read by this module under any circumstance.
_API_KEY_ENV_VAR = "CONTEXT_INTELLIGENCE_PERSONAL"

_UPLOAD_COMMAND = "context-intelligence-upload"
_UPLOAD_TIMEOUT_SECONDS = 300


@dataclass
class UploadOutcome:
    """Outcome of one best-effort upload attempt -- always returned, never raised.

    ``attempted=False`` covers every "we didn't even try" case (no API key
    configured, the CLI isn't installed, the session directory doesn't
    exist yet) -- these are configuration facts, not upload failures, but
    are still recorded so a silently-missing API key doesn't masquerade as
    "everything's fine, nothing to upload."
    """

    attempted: bool
    exit_code: int | None  # None only when attempted is False
    error: str | None  # None exactly when exit_code == 0

    def to_dict(self) -> dict[str, bool | int | str | None]:
        return asdict(self)


def upload_session(session_dir: Path, *, job_id: str) -> UploadOutcome:
    """Best-effort upload of one session directory's captured events.

    ``session_dir`` should be the specific
    ``.../sessions/<session_id>/context-intelligence`` directory (or the
    ``<session_id>`` directory itself -- the CLI recurses) for the ONE
    session this run just used, not the whole workspace tree -- scoping to
    one run's session keeps each upload fast and keeps a failure
    attributable to a specific run.

    Every POST the underlying CLI makes carries a content-addressed
    idempotency key, so re-uploading the same session (e.g. retried after a
    prior failure) is always safe and never creates duplicate graph data.
    """
    api_key = os.environ.get(_API_KEY_ENV_VAR, "").strip()
    if not api_key:
        return UploadOutcome(
            attempted=False,
            exit_code=None,
            error=f"${_API_KEY_ENV_VAR} is not set in the environment -- upload skipped",
        )

    binary = shutil.which(_UPLOAD_COMMAND)
    if binary is None:
        return UploadOutcome(
            attempted=False,
            exit_code=None,
            error=(
                f"{_UPLOAD_COMMAND!r} not found on PATH -- upload skipped "
                "(install with: uv tool install "
                '"amplifier-module-tool-context-intelligence-upload @ '
                "git+https://github.com/microsoft/amplifier-bundle-context-intelligence"
                '@main#subdirectory=modules/tool-context-intelligence-upload")'
            ),
        )

    if not session_dir.is_dir():
        return UploadOutcome(
            attempted=False,
            exit_code=None,
            error=f"session directory not found: {session_dir} -- upload skipped",
        )

    cmd = [
        binary,
        "--path",
        str(session_dir),
        "--server-url",
        _SERVER_URL,
        "--api-key",
        api_key,
        "--job-id",
        job_id,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_UPLOAD_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return UploadOutcome(
            attempted=True,
            exit_code=None,
            error=f"upload timed out after {_UPLOAD_TIMEOUT_SECONDS}s: {exc}",
        )
    except OSError as exc:
        return UploadOutcome(
            attempted=True,
            exit_code=None,
            error=f"failed to invoke {_UPLOAD_COMMAND}: {exc}",
        )

    if proc.returncode == 0:
        return UploadOutcome(attempted=True, exit_code=0, error=None)

    # Exit codes for the default (context-intelligence) format: 1 = at
    # least one HTTP error occurred, 2 = invalid invocation (bad --path,
    # nothing found to upload). Recorded verbatim rather than
    # re-interpreted -- the real stderr tail is more useful than a guess
    # at which category applies.
    stderr_tail = (proc.stderr or "").strip()
    if len(stderr_tail) > 500:
        stderr_tail = stderr_tail[-500:]
    return UploadOutcome(
        attempted=True,
        exit_code=proc.returncode,
        error=stderr_tail or f"exited {proc.returncode} with no stderr output",
    )


__all__ = ["UploadOutcome", "upload_session"]
