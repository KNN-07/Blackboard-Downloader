"""Release checks and verified, detached native update handoffs.

Only frozen applications can install updates. Helpers preserve a backup and write
``install.log`` in the private download directory; errors after the GUI exits are
recorded there and the previous application is restarted whenever possible.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


_REPO = "KNN-07/Blackboard-Downloader"
_API = f"https://api.github.com/repos/{_REPO}/releases/latest"
_MAX_DOWNLOAD = 1024 * 1024 * 1024
_VERSION = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_verified: dict[Path, tuple[str, int, str]] = {}


class UpdateError(Exception):
    """An update could not be checked, downloaded, or handed off safely."""


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    url: str
    asset_name: str
    asset_url: str
    sha256: str
    size: int


def _version(value: str) -> tuple[tuple[int, int, int], tuple[str, ...] | None]:
    match = _VERSION.fullmatch(value)
    if not match:
        raise UpdateError(f"Invalid semantic version: {value!r}")
    pre = tuple(match[4].split(".")) if match[4] else None
    if pre and any(not item or (item.isdigit() and len(item) > 1 and item[0] == "0") for item in pre):
        raise UpdateError(f"Invalid semantic version: {value!r}")
    if match[5] and any(not item for item in match[5].split(".")):
        raise UpdateError(f"Invalid semantic version: {value!r}")
    return (int(match[1]), int(match[2]), int(match[3])), pre


def _newer(candidate: str, current: str) -> bool:
    new_core, new_pre = _version(candidate)
    old_core, old_pre = _version(current)
    if new_core != old_core:
        return new_core > old_core
    if new_pre is None or old_pre is None:
        return new_pre is None and old_pre is not None
    for left, right in zip(new_pre, old_pre):
        if left == right:
            continue
        if left.isdigit() and right.isdigit():
            return int(left) > int(right)
        if left.isdigit() != right.isdigit():
            return not left.isdigit()
        return left > right
    return len(new_pre) > len(old_pre)


def _trusted_url(url: str, *, api: bool = False, redirect: bool = False) -> bool:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        return False
    if api:
        return parsed.hostname == "api.github.com" and parsed.path == f"/repos/{_REPO}/releases/latest"
    if parsed.hostname == "github.com":
        return parsed.path.startswith(f"/{_REPO}/releases/download/")
    return redirect and parsed.hostname in {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _trusted_url(newurl, redirect=True):
            raise UpdateError("Release download redirected to an untrusted host.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, *, api: bool = False):
    if not _trusted_url(url, api=api):
        raise UpdateError("Invalid release URL.")
    # This independent opener never inherits Blackboard cookies or credentials.
    opener = urllib.request.build_opener(_Redirects())
    request = urllib.request.Request(url, headers={
        "User-Agent": "Blackboard-Downloader-Updater",
        "Accept": "application/vnd.github+json" if api else "application/octet-stream",
        "Accept-Encoding": "identity",
    })
    try:
        response = opener.open(request, timeout=15)
        if response.status != 200 or response.headers.get("Content-Encoding", "identity") != "identity":
            response.close()
            raise UpdateError("Unexpected release server response.")
        return response
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise UpdateError(f"Release request failed: {exc}") from exc


def _fetch(url: str, limit: int, *, api: bool = False) -> bytes:
    deadline = time.monotonic() + 60
    chunks = []
    total = 0
    with _open(url, api=api) as response:
        while True:
            chunk = response.read(min(65536, limit + 1 - total))
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > limit or time.monotonic() > deadline:
                raise UpdateError("Release metadata exceeds its size or time limit.")
            chunks.append(chunk)


def _helper_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("_PYI_")}
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for key in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        original = environment.pop(key + "_ORIG", None)
        if original is None:
            environment.pop(key, None)
        else:
            environment[key] = original
    return environment


def _installation() -> tuple[str, Path]:
    if not getattr(sys, "frozen", False):
        return "source", Path(sys.executable)
    executable = Path(sys.executable).absolute()
    if executable.is_symlink():
        return "unsupported", executable
    machine = platform.machine().lower()
    if sys.platform == "win32" and machine in {"amd64", "x86_64"}:
        mode = "windows-setup" if (executable.parent / "unins000.exe").is_file() else "windows-portable"
        return (mode if os.access(executable.parent, os.W_OK) else "unsupported"), executable
    if sys.platform == "darwin" and machine in {"arm64", "aarch64", "x86_64", "amd64"}:
        for parent in executable.parents:
            if parent.suffix == ".app" and executable == parent / "Contents/MacOS/Blackboard Downloader":
                if not parent.is_symlink() and os.access(parent.parent, os.W_OK) and not str(parent).startswith("/Volumes/"):
                    return "macos", parent
        return "unsupported", executable
    if sys.platform.startswith("linux") and machine in {"amd64", "x86_64"}:
        if executable == Path("/usr/bin/blackboard-downloader"):
            if shutil.which("dpkg-query") and shutil.which("pkexec"):
                try:
                    owner = subprocess.run(["dpkg-query", "-S", str(executable)], capture_output=True, text=True, timeout=5, env=_helper_environment())
                    if owner.returncode == 0 and owner.stdout.strip() == "blackboard-downloader: /usr/bin/blackboard-downloader":
                        return "linux-deb", executable
                except (OSError, subprocess.SubprocessError):
                    pass
            return "unsupported", executable
        if os.access(executable.parent, os.W_OK) and os.access(executable, os.W_OK):
            return "linux-portable", executable
    return "unsupported", executable


def automatic_install_supported() -> bool:
    """Whether the current frozen installation supports a safe native handoff."""
    return _installation()[0] not in {"source", "unsupported"}


def _asset_name(version: str) -> str:
    mode, _ = _installation()
    machine = platform.machine().lower()
    if sys.platform == "win32" and machine in {"amd64", "x86_64"}:
        suffix = "-Setup" if mode == "windows-setup" else ""
        return f"Blackboard-Downloader-{version}-Windows-x64{suffix}.exe"
    if sys.platform == "darwin" and machine in {"arm64", "aarch64", "amd64", "x86_64"}:
        arch = "arm64" if machine in {"arm64", "aarch64"} else "x86_64"
        return f"Blackboard-Downloader-{version}-macOS-{arch}.dmg"
    if sys.platform.startswith("linux") and machine in {"amd64", "x86_64"}:
        if mode == "linux-deb":
            return f"blackboard-downloader_{version}_amd64.deb"
        return f"Blackboard-Downloader-{version}-Linux-x64.tar.gz"
    return ""


def check_release(current_version: str) -> Release | None:
    """Find a newer stable GitHub release, never a draft or prerelease."""
    _version(current_version)
    try:
        try:
            data = json.loads(_fetch(_API, 2 * 1024 * 1024, api=True))
        except UpdateError as exc:
            if isinstance(exc.__cause__, urllib.error.HTTPError) and exc.__cause__.code == 404:
                return None
            raise
        if not isinstance(data, dict) or data.get("draft") is not False or data.get("prerelease") is not False:
            raise UpdateError("Invalid stable release metadata.")
        tag = data.get("tag_name", "")
        version = tag.removeprefix("v")
        if _version(version)[1] is not None or not _newer(version, current_version):
            return None
        url = f"https://github.com/{_REPO}/releases/tag/{urllib.parse.quote(tag, safe='')}"
        name = _asset_name(version)
        notes = data.get("body") or ""
        if not isinstance(notes, str):
            raise UpdateError("Invalid release notes.")
        # Unsupported machines can still display release notifications.
        if not name:
            return Release(version, notes, url, "", "", "", 0)
        assets = data.get("assets", [])
        selected = [asset for asset in assets if asset.get("name") == name]
        if len(selected) != 1:
            raise UpdateError(f"Release does not contain exactly one compatible asset: {name}")
        asset = selected[0]
        asset_url = asset["browser_download_url"]
        size = asset["size"]
        if not _trusted_url(asset_url) or type(size) is not int or not 0 < size <= _MAX_DOWNLOAD:
            raise UpdateError("Invalid release asset URL or size.")
        expected_path = f"/{_REPO}/releases/download/{tag}/{name}"
        if urllib.parse.unquote(urllib.parse.urlsplit(asset_url).path) != expected_path:
            raise UpdateError("Asset URL does not match its release and filename.")
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:") and _SHA256.fullmatch(digest[7:]):
            checksum = digest[7:].lower()
        else:
            manifests = [entry for entry in assets if entry.get("name") == "SHA256SUMS"]
            if len(manifests) != 1:
                raise UpdateError("Release has no trusted SHA-256 checksum.")
            manifest_url = manifests[0]["browser_download_url"]
            if not _trusted_url(manifest_url) or urllib.parse.unquote(urllib.parse.urlsplit(manifest_url).path) != f"/{_REPO}/releases/download/{tag}/SHA256SUMS":
                raise UpdateError("Invalid checksum manifest URL.")
            checksums = []
            for line in _fetch(manifest_url, 128 * 1024).decode("utf-8").splitlines():
                match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line)
                if match and match[2] == name:
                    checksums.append(match[1].lower())
            if len(checksums) != 1:
                raise UpdateError("Missing or ambiguous release checksum.")
            checksum = checksums[0]
        return Release(version, notes, url, name, asset_url, checksum, size)
    except UpdateError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise UpdateError(f"Invalid release metadata: {exc}") from exc


def download_update(release: Release, cancel: threading.Event, progress: Callable[[int, int], None]) -> Path:
    """Stream an asset into a private staging directory and verify it before use."""
    if release.asset_name != _asset_name(release.version) or not release.asset_name or not _SHA256.fullmatch(release.sha256) or not 0 < release.size <= _MAX_DOWNLOAD:
        raise UpdateError("Invalid or incompatible update asset.")
    directory = Path(tempfile.mkdtemp(prefix="blackboard-update-"))
    path = directory / release.asset_name
    digest = hashlib.sha256()
    received = 0
    deadline = time.monotonic() + 30 * 60
    try:
        if cancel.is_set():
            raise UpdateError("Update download cancelled.")
        with _open(release.asset_url) as response, path.open("xb") as output:
            length = response.headers.get("Content-Length")
            if length is not None and int(length) != release.size:
                raise UpdateError("Release download size does not match metadata.")
            progress(0, release.size)
            while True:
                if cancel.is_set():
                    raise UpdateError("Update download cancelled.")
                if time.monotonic() > deadline:
                    raise UpdateError("Update download timed out.")
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > release.size:
                    raise UpdateError("Release download exceeds its declared size.")
                output.write(chunk)
                digest.update(chunk)
                progress(received, release.size)
            output.flush()
            os.fsync(output.fileno())
        if cancel.is_set():
            raise UpdateError("Update download cancelled.")
        if received != release.size or digest.hexdigest() != release.sha256.lower():
            raise UpdateError("Update integrity verification failed; nothing was installed.")
        _verified[path] = (release.sha256.lower(), release.size, release.asset_name)
        return path
    except Exception as exc:
        shutil.rmtree(directory, ignore_errors=True)
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(f"Update download failed: {exc}") from exc


def _verify_staged(path: Path) -> None:
    expected = _verified.get(path)
    if expected is None or path.is_symlink() or not path.is_file():
        raise UpdateError("Only a verified download from this application session can be installed.")
    digest, size, name = expected
    if path.name != name or path.stat().st_size != size:
        raise UpdateError("Staged update changed after verification.")
    with path.open("rb") as stream:
        verifier = hashlib.sha256()
        while chunk := stream.read(256 * 1024):
            verifier.update(chunk)
        actual = verifier.hexdigest()
    if actual != digest:
        raise UpdateError("Staged update changed after verification.")


_UNIX_HELPER = r'''#!/bin/sh
set -u
pid=$1; target=$2; staged=$3; backup=$4; ready=$5; mode=$6; payload=$7; privileged=$8
restart() {
    if [ "$mode" = macos ]; then /usr/bin/open "$target"; else "$target" </dev/null >/dev/null 2>&1 & fi
}
fail() {
    echo "Update failed: $*; original application retained. See this install.log."
    restart
    exit 1
}
printf ready > "$ready" || exit 1
count=0
while kill -0 "$pid" 2>/dev/null; do
    count=$((count + 1))
    [ "$count" -lt 240 ] || { echo 'Parent did not exit; update cancelled.'; exit 1; }
    sleep 1
done
if [ "$mode" = linux-deb ]; then
    pkexec /bin/sh "$privileged" "$payload" "$target" "$backup" || { fail 'Package installation or authorization failed'; }
else
    mv "$target" "$backup" || { fail 'Cannot back up application'; }
    if ! mv "$staged" "$target"; then
        mv "$backup" "$target" || { echo "RESTORE FAILED: original remains at $backup"; exit 1; }
        fail 'Cannot replace application'
    fi
fi
restart
echo "Update installed. Backup retained at $backup"
'''

_DEB_HELPER = r'''#!/bin/sh
set -eu
payload=$1; target=$2; backup=$3
# dpkg runs only after the unprivileged helper has observed the GUI exit.
cp -p "$target" "$backup"
if ! /usr/bin/dpkg --install "$payload"; then
    cp -p "$backup" "$target"
    echo 'dpkg failed; original executable restored. Package state may require sudo apt --fix-broken install.'
    exit 1
fi
'''

_WINDOWS_HELPER = r'''param([int]$ParentPid, [string]$Target, [string]$Payload, [string]$Backup, [string]$Ready, [string]$Mode)
$ErrorActionPreference = 'Stop'
Set-Content -LiteralPath $Ready -Value 'ready'
try {
    $parent = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
    if ($parent -and !$parent.WaitForExit(240000)) { Write-Output 'Parent did not exit; update cancelled'; exit 1 }
    # A PyInstaller onefile bootloader can outlive its Python child briefly.
    # Do not touch the executable until Windows releases its image/file locks.
    $unlocked = $false
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        try {
            $handle = [System.IO.File]::Open($Target, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
            $handle.Dispose()
            $unlocked = $true
            break
        } catch [System.IO.IOException] {
            Start-Sleep -Milliseconds 250
        }
    }
    if (!$unlocked) { throw 'Application executable remained locked; original retained' }
    Copy-Item -LiteralPath $Target -Destination $Backup
    try {
        if ($Mode -eq 'windows-setup') {
            $directory = Split-Path -Parent $Target
            $arguments = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /DIR="' + $directory + '"'
            $installer = Start-Process -FilePath $Payload -ArgumentList $arguments -Wait -PassThru
            if ($installer.ExitCode -ne 0) { throw ('Installer exit code: ' + $installer.ExitCode) }
        } else {
            Move-Item -LiteralPath $Payload -Destination $Target -Force
        }
    } catch {
        Copy-Item -LiteralPath $Backup -Destination $Target -Force
        throw
    }
    Start-Process -FilePath $Target
    Write-Output "Update installed. Backup retained at $Backup"
} catch {
    Write-Output $_
    if (Test-Path -LiteralPath $Target) { Start-Process -FilePath $Target }
    exit 1
}
'''


def _prepare_macos(path: Path, staged: Path) -> None:
    mount = path.parent / "mount"
    mount.mkdir()
    attached = False
    try:
        result = subprocess.run(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-plist", "-mountpoint", str(mount), str(path)], capture_output=True, timeout=120, env=_helper_environment())
        if result.returncode:
            raise UpdateError("Unable to mount the verified macOS update.")
        attached = True
        plistlib.loads(result.stdout)
        app = mount / "Blackboard Downloader.app"
        executable = app / "Contents/MacOS/Blackboard Downloader"
        if app.is_symlink() or not executable.is_file() or executable.is_symlink():
            raise UpdateError("Disk image has no valid application bundle.")
        for root, directories, files in os.walk(app):
            for name in directories + files:
                item = Path(root) / name
                if item.is_symlink() and not item.resolve().is_relative_to(app.resolve()):
                    raise UpdateError("Application bundle contains an escaping symbolic link.")
        shutil.copytree(app, staged, symlinks=True)
    finally:
        if attached:
            subprocess.run(["/usr/bin/hdiutil", "detach", str(mount)], capture_output=True, timeout=30, env=_helper_environment())


def install_update(path: Path) -> None:
    """Prepare a verified update and launch a helper that waits for this process.

    Returning permits the GUI to exit; synchronous preparation/launch failures
    raise UpdateError without modifying the installed application. Later helper
    failures are logged and roll back the executable/application bundle. Debian
    package-manager state may still need repair after a failed dpkg transaction.
    """
    mode, target = _installation()
    if mode in {"source", "unsupported"}:
        raise UpdateError("Automatic installation requires a supported writable packaged installation; source checkouts are never modified.")
    path = Path(path).absolute()
    staged = None
    process = None
    try:
        _verify_staged(path)
        expected = _verified[path][2]
        valid_suffix = {"macos": ".dmg", "linux-deb": ".deb", "linux-portable": ".tar.gz", "windows-setup": "-Setup.exe", "windows-portable": ".exe"}[mode]
        if not expected.endswith(valid_suffix) or (mode == "windows-portable" and expected.endswith("-Setup.exe")):
            raise UpdateError("Downloaded asset does not match this installation.")
        token = path.parent.name.removeprefix("blackboard-update-")
        backup = target.with_name(target.name + ".backup-" + token)
        if os.path.lexists(backup):
            raise UpdateError("Update backup path already exists.")
        if mode in {"linux-portable", "windows-portable", "macos"}:
            candidate = target.with_name(target.name + ".update-" + token)
            if os.path.lexists(candidate):
                raise UpdateError("Update staging path already exists.")
            staged = candidate
        ready = path.parent / "helper.ready"
        ready.unlink(missing_ok=True)
        if mode == "linux-portable":
            with tarfile.open(path, "r:gz") as archive:
                member = archive.next()
                if member is None or member.name != "blackboard-downloader" or not member.isreg() or member.sparse is not None or not 0 < member.size <= _MAX_DOWNLOAD:
                    raise UpdateError("Linux update archive contains unexpected paths or file types.")
                source = archive.extractfile(member)
                if source is None:
                    raise UpdateError("Linux update archive has no executable.")
                with source, staged.open("xb") as output:
                    shutil.copyfileobj(source, output, 256 * 1024)
                if archive.next() is not None:
                    raise UpdateError("Linux update archive contains additional files.")
                staged.chmod(stat.S_IMODE(target.stat().st_mode) & 0o777)
        elif mode == "macos":
            _prepare_macos(path, staged)
        elif mode == "windows-portable":
            with path.open("rb") as source, staged.open("xb") as output:
                shutil.copyfileobj(source, output, 256 * 1024)
        log = path.parent / "install.log"
        if mode.startswith("windows"):
            helper = path.parent / "install.ps1"
            helper.write_text(_WINDOWS_HELPER, encoding="utf-8")
            powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            command = [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(helper), "-ParentPid", str(os.getpid()), "-Target", str(target), "-Payload", str(staged or path), "-Backup", str(backup), "-Ready", str(ready), "-Mode", mode]
            options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            helper = path.parent / "install.sh"
            helper.write_text(_UNIX_HELPER, encoding="utf-8")
            privileged = path.parent / "package-install.sh"
            if mode == "linux-deb":
                privileged.write_text(_DEB_HELPER, encoding="utf-8")
            command = ["/bin/sh", str(helper), str(os.getpid()), str(target), str(staged or path), str(backup), str(ready), mode, str(path), str(privileged)]
            options = {"start_new_session": True}
        with log.open("ab") as output:
            # PyInstaller's DLL search directory is inherited by Windows children.
            # Restore it immediately after launching the native system helper.
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.kernel32.SetDllDirectoryW(None)
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, cwd=path.parent, close_fds=True, env=_helper_environment(), **options)
            finally:
                if sys.platform == "win32":
                    ctypes.windll.kernel32.SetDllDirectoryW(getattr(sys, "_MEIPASS", None))
        deadline = time.monotonic() + 10
        while not ready.is_file():
            if process.poll() is not None or time.monotonic() > deadline:
                raise UpdateError(f"Update helper could not start; see {log}")
            time.sleep(0.05)
        if process.poll() is not None:
            raise UpdateError(f"Update helper exited unexpectedly; see {log}")
        _verified.pop(path, None)
    except Exception as exc:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        if staged is not None and staged.exists():
            if staged.is_dir():
                shutil.rmtree(staged, ignore_errors=True)
            else:
                staged.unlink(missing_ok=True)
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(f"Update installation could not start: {exc}") from exc
