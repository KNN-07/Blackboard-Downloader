from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Dependency:
    import_name: str
    package_name: str
    display_name: str


REQUIRED_DEPENDENCIES = (
    Dependency("requests", "requests", "Requests"),
    Dependency("bs4", "beautifulsoup4", "Beautiful Soup"),
    Dependency("selenium", "selenium", "Selenium browser support"),
    Dependency("keyring", "keyring", "Secure credential storage"),
    Dependency("PIL", "Pillow", "School logo support"),
    Dependency("cryptography", "cryptography", "Encrypted session storage"),
)


def audit_dependencies() -> list[Dependency]:
    """Return required runtime dependencies that cannot be imported."""
    return [
        dependency
        for dependency in REQUIRED_DEPENDENCIES
        if importlib.util.find_spec(dependency.import_name) is None
    ]


def requirements_path() -> Path:
    return Path(__file__).resolve().parent.parent / "requirements.txt"


def install_command() -> str:
    return f'"{sys.executable}" -m pip install -r "{requirements_path()}"'
