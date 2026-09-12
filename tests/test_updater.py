import hashlib
import io
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from blackboard_gui import updater


class UpdateSafetyTest(unittest.TestCase):
    def test_stable_release_supersedes_same_core_prerelease_not_newer_core(self):
        self.assertTrue(updater._newer("1.2.3", "1.2.3-rc.9"))
        self.assertFalse(updater._newer("1.2.3", "1.3.0-alpha"))
        self.assertFalse(updater._newer("1.2.3+new", "1.2.3+old"))
        self.assertTrue(updater._newer("1.2.3-rc.10", "1.2.3-rc.2"))
        with self.assertRaises(updater.UpdateError):
            updater._newer("1.2.3-01", "1.2.2")

    def test_corrupt_stream_is_removed_and_cannot_be_installed(self):
        payload = b"corrupted"
        response = io.BytesIO(payload)
        response.headers = {"Content-Length": str(len(payload))}
        release = updater.Release("2.0.0", "", "", "update.exe", "https://github.com/KNN-07/Blackboard-Downloader/releases/download/v2.0.0/update.exe", "0" * 64, len(payload))
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "download"
            staging.mkdir()
            with patch.object(updater, "_asset_name", return_value="update.exe"), patch.object(updater, "_open", return_value=response), patch.object(updater.tempfile, "mkdtemp", return_value=str(staging)):
                with self.assertRaisesRegex(updater.UpdateError, "integrity"):
                    updater.download_update(release, threading.Event(), lambda *_: None)
            self.assertFalse(staging.exists())

    def test_tampered_verified_payload_cannot_launch_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "update.exe"
            path.write_bytes(b"original")
            updater._verified[path] = (hashlib.sha256(b"original").hexdigest(), 8, path.name)
            self.addCleanup(updater._verified.pop, path, None)
            path.write_bytes(b"tampered")
            with patch.object(updater, "_installation", return_value=("windows-portable", Path(directory) / "app.exe")), patch.object(updater.subprocess, "Popen") as launch:
                with self.assertRaisesRegex(updater.UpdateError, "changed"):
                    updater.install_update(path)
                launch.assert_not_called()

    def test_archive_traversal_does_not_modify_original_or_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app"
            target.write_bytes(b"original")
            staging = root / "download"
            staging.mkdir()
            path = staging / "update.tar.gz"
            with tarfile.open(path, "w:gz") as archive:
                member = tarfile.TarInfo("../escaped")
                member.size = 3
                archive.addfile(member, io.BytesIO(b"bad"))
            updater._verified[path] = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size, path.name)
            self.addCleanup(updater._verified.pop, path, None)
            with patch.object(updater, "_installation", return_value=("linux-portable", target)), patch.object(updater.subprocess, "Popen") as launch:
                with self.assertRaisesRegex(updater.UpdateError, "unexpected paths"):
                    updater.install_update(path)
                launch.assert_not_called()
            self.assertEqual(target.read_bytes(), b"original")
            self.assertFalse((root / "escaped").exists())

    def test_source_install_is_rejected_before_touching_payload(self):
        with patch.object(updater, "_installation", return_value=("source", Path("python"))):
            with self.assertRaisesRegex(updater.UpdateError, "source checkouts"):
                updater.install_update(Path("not-a-real-payload"))

    def test_foreign_release_urls_are_rejected(self):
        for url in (
            "http://github.com/KNN-07/Blackboard-Downloader/releases/download/v2/x",
            "https://github.com/attacker/project/releases/download/v2/x",
            "https://github.com.evil.invalid/KNN-07/Blackboard-Downloader/releases/download/v2/x",
            "https://user:password@github.com/KNN-07/Blackboard-Downloader/releases/download/v2/x",
        ):
            with self.subTest(url=url):
                self.assertFalse(updater._trusted_url(url))


if __name__ == "__main__":
    unittest.main()
