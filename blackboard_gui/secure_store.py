from __future__ import annotations

import json
import hashlib
import os
import sys
from pathlib import Path


SERVICE_NAME = "Blackboard Downloader"


class SessionStoreError(RuntimeError):
    pass


class SessionStore:
    """Encrypts cookies on disk with a key held by the OS credential vault."""

    @staticmethod
    def _keyring():
        try:
            import keyring
        except ImportError as exc:
            raise SessionStoreError(
                "Secure session storage is unavailable because keyring is not installed."
            ) from exc
        backend = keyring.get_keyring()
        if getattr(backend, "priority", 0) <= 0:
            raise SessionStoreError(
                "No secure credential vault is available on this system."
            )
        return keyring

    def load(self, base_url: str) -> list[dict]:
        try:
            from cryptography.fernet import Fernet, InvalidToken
        except ImportError as exc:
            raise SessionStoreError(
                "Encrypted session storage is unavailable because cryptography is not installed."
            ) from exc

        try:
            session_path = self._session_path(base_url)
            if not session_path.exists():
                return []
            key = self._keyring().get_password(
                SERVICE_NAME, self._credential_name(base_url)
            )
            if not key:
                return []
            payload = Fernet(key.encode("ascii")).decrypt(session_path.read_bytes())
            decoded = json.loads(payload.decode("utf-8"))
            return decoded if isinstance(decoded, list) else []
        except SessionStoreError:
            raise
        except (InvalidToken, ValueError, UnicodeError) as exc:
            raise SessionStoreError(
                "The saved Blackboard session is damaged or belongs to another user."
            ) from exc
        except Exception as exc:
            raise SessionStoreError(
                f"The saved Blackboard session could not be read: {exc}"
            ) from exc

    def save(self, base_url: str, cookies: list[dict]) -> None:
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise SessionStoreError(
                "Encrypted session storage is unavailable because cryptography is not installed."
            ) from exc

        try:
            keyring = self._keyring()
            credential_name = self._credential_name(base_url)
            key = keyring.get_password(SERVICE_NAME, credential_name)
            if not key:
                key = Fernet.generate_key().decode("ascii")
                keyring.set_password(SERVICE_NAME, credential_name, key)
            payload = json.dumps(cookies, separators=(",", ":")).encode("utf-8")
            encrypted = Fernet(key.encode("ascii")).encrypt(payload)
            session_path = self._session_path(base_url)
            session_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = session_path.with_suffix(".tmp")
            temporary.write_bytes(encrypted)
            temporary.replace(session_path)
            if os.name != "nt":
                session_path.chmod(0o600)
        except SessionStoreError:
            raise
        except Exception as exc:
            raise SessionStoreError(
                f"The Blackboard session could not be saved securely: {exc}"
            ) from exc

    def delete(self, base_url: str) -> None:
        try:
            keyring = self._keyring()
            credential_name = self._credential_name(base_url)
            self._session_path(base_url).unlink(missing_ok=True)
            if keyring.get_password(SERVICE_NAME, credential_name) is not None:
                keyring.delete_password(SERVICE_NAME, credential_name)
        except SessionStoreError:
            raise
        except Exception as exc:
            raise SessionStoreError("The saved Blackboard session could not be removed.") from exc

    @staticmethod
    def _credential_name(base_url: str) -> str:
        digest = hashlib.sha256(base_url.encode("utf-8")).hexdigest()
        return f"session-key:{digest}"

    @classmethod
    def _session_path(cls, base_url: str) -> Path:
        digest = hashlib.sha256(base_url.encode("utf-8")).hexdigest()
        if sys.platform == "win32":
            root = Path(os.environ.get("LOCALAPPDATA", os.environ.get("APPDATA", Path.home())))
            root = root / "Blackboard Downloader"
        elif sys.platform == "darwin":
            root = Path.home() / "Library" / "Application Support" / "Blackboard Downloader"
        else:
            data_root = Path(
                os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
            )
            root = data_root / "blackboard-downloader"
        return root / "sessions" / f"{digest}.bin"
