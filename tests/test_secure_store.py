import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from blackboard_gui.secure_store import SERVICE_NAME, SessionStore


class SecureStoreTest(unittest.TestCase):
    @patch.object(SessionStore, "_keyring")
    def test_save_serializes_cookies_to_platform_vault(self, keyring_factory):
        keyring = Mock()
        keyring.get_password.return_value = None
        keyring_factory.return_value = keyring
        cookies = [{"name": "session", "value": "secret"}]
        with TemporaryDirectory() as directory:
            session_path = Path(directory) / "session.bin"
            with patch.object(SessionStore, "_session_path", return_value=session_path):
                SessionStore().save("https://learn.example.edu", cookies)
            args = keyring.set_password.call_args.args
            self.assertEqual(args[0], SERVICE_NAME)
            self.assertTrue(args[1].startswith("session-key:"))
            decrypted = Fernet(args[2].encode("ascii")).decrypt(
                session_path.read_bytes()
            )
            self.assertIn(b'"session"', decrypted)

    @patch.object(SessionStore, "_keyring")
    def test_load_deserializes_saved_cookies(self, keyring_factory):
        keyring = Mock()
        key = Fernet.generate_key()
        keyring.get_password.return_value = key.decode("ascii")
        keyring_factory.return_value = keyring
        with TemporaryDirectory() as directory:
            session_path = Path(directory) / "session.bin"
            session_path.write_bytes(
                Fernet(key).encrypt(b'[{"name":"session","value":"secret"}]')
            )
            with patch.object(SessionStore, "_session_path", return_value=session_path):
                cookies = SessionStore().load("https://learn.example.edu")
            self.assertEqual(cookies[0]["value"], "secret")


if __name__ == "__main__":
    unittest.main()
