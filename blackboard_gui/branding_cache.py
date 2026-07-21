from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from .client import SchoolBranding


class BrandingCache:
    """Caches public institution branding captured from Blackboard's rendered UI."""

    def load(self, base_url: str) -> SchoolBranding | None:
        metadata_path, logo_path = self._paths(base_url)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            name = metadata.get("name")
            if not isinstance(name, str) or not name.strip():
                return None
            logo_bytes = logo_path.read_bytes() if logo_path.exists() else None
            return SchoolBranding(name.strip(), logo_bytes)
        except (OSError, ValueError):
            return None

    def save(self, base_url: str, branding: SchoolBranding) -> None:
        metadata_path, logo_path = self._paths(base_url)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps({"name": branding.name}, indent=2), encoding="utf-8"
        )
        if branding.logo_bytes:
            logo_path.write_bytes(branding.logo_bytes)

    def delete(self, base_url: str) -> None:
        metadata_path, logo_path = self._paths(base_url)
        metadata_path.unlink(missing_ok=True)
        logo_path.unlink(missing_ok=True)

    @classmethod
    def _paths(cls, base_url: str) -> tuple[Path, Path]:
        digest = hashlib.sha256(base_url.encode("utf-8")).hexdigest()
        root = cls._root() / digest
        return root / "branding.json", root / "logo.png"

    @staticmethod
    def _root() -> Path:
        if sys.platform == "win32":
            base = Path(
                os.environ.get("LOCALAPPDATA", os.environ.get("APPDATA", Path.home()))
            ) / "Blackboard Downloader"
        elif sys.platform == "darwin":
            base = (
                Path.home()
                / "Library"
                / "Application Support"
                / "Blackboard Downloader"
            )
        else:
            data_root = Path(
                os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
            )
            base = data_root / "blackboard-downloader"
        return base / "branding"
