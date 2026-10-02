"""Project-local environment loading for SolarGrid AI.

The application always resolves ``.env`` relative to this source tree instead of
relying on the shell's current working directory.  This keeps local Windows
startup predictable whether the app is started from the project root, an IDE,
or another working directory.

Secret values are never hard-coded here; this module only loads them into the
process environment.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"


def load_project_env(*, override: bool = False) -> bool:
    """Load the project ``.env`` file if present.

    Existing OS-level environment variables win by default, which is the safest
    behavior for deployments and CI.  Returns ``True`` when python-dotenv found
    and loaded the project file, otherwise ``False``.
    """
    return bool(load_dotenv(dotenv_path=ENV_FILE, override=override))


def env_value(name: str, default: str | None = None) -> str | None:
    """Return a trimmed environment value, treating an empty value as missing."""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip()
    return value if value else default


def secret_is_configured(name: str) -> bool:
    """Return whether a secret-like variable contains a non-placeholder value."""
    value = env_value(name)
    if not value:
        return False
    lowered = value.lower()
    placeholder_markers = (
        "your_real_",
        "your_api_key",
        "change-me",
        "put-your-",
        "ضع_مفتاح",
    )
    return not any(marker in lowered for marker in placeholder_markers)
