"""Suite-wide isolation from the developer's own agent environment.

The engine reads real environment variables that a developer machine legitimately
sets for its OWN running deployment -- above all ``$AMPLIFIER_AGENT_CONFIG``,
which points at that machine's agent host config. A unit test that reads it is
testing the developer's laptop, not the code: the suite passes or fails
depending on a file no fixture wrote, and CI and a workstation disagree for
reasons neither can see.

So every test runs with those variables cleared. A test that wants one sets it
itself, explicitly, and owns the file it points at.
"""

from __future__ import annotations

import os

import pytest

# Cleared for every test. Each is read by production code from the ambient
# environment, and each has a machine-specific real value on a developer box.
_SCRUBBED = (
    "AMPLIFIER_AGENT_CONFIG",
    "AMPLIFIER_AGENT_WORKSPACE",
    "AMPLIFIER_AGENT_PROVIDER",
    "AMPLIFIER_AGENT_MODEL",
    "AMPLIFIER_AGENT_STORAGE",
    "DRUMBEAT_SESSION_ROTATE_TOKENS",
    "DRUMBEAT_DATA_DIR",
)


@pytest.fixture(autouse=True)
def _hermetic_agent_env() -> None:
    saved = {name: os.environ.pop(name, None) for name in _SCRUBBED}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
