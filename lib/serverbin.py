"""Downloads and installs the stremio-server-go binary from GitHub releases.

Pure Python (no Kodi imports) so this module can be exercised directly with
plain python3. lib/service_runner.py's resolve_binary() looks for the
binary at ``<addon_data_dir>/bin/stremio-server`` (``.exe`` on Windows) --
install_binary() targets that exact location.

Asset naming follows M0Rf30/stremio-server-go's .goreleaser.yml
`archives.name_template`:
    stremio-server_{Os-titlecased}_{arch}[v{goarm}].{tar.gz|zip}
e.g. stremio-server_Linux_x86_64.tar.gz, stremio-server_Windows_arm64.zip,
stremio-server_Linux_armv7.tar.gz. Download URLs and asset names are
derived deterministically from GITHUB_REPO/SERVER_TAG/asset-name -- this
module never queries the "latest release" API or a same-release
checksums.txt at runtime; see PINNED_SHA256 below for why.
"""
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile

try:
    import requests
except ImportError:  # pragma: no cover - exercised only without the dependency
    requests = None  # type: ignore[assignment]

GITHUB_REPO = "M0Rf30/stremio-server-go"
SERVER_TAG = "v0.12.0"
USER_AGENT = "plugin.video.rivulet"

BINARY_NAME = "stremio-server"
PART_SUFFIX = ".part"
DOWNLOAD_CHUNK_SIZE = 64 * 1024
REQUEST_TIMEOUT = 30
VERIFY_TIMEOUT = 15

# SHA-256 digests for every stremio-server-go SERVER_TAG release asset,
# hand-verified against that tag's GitHub release page + checksums.txt on
# 2026-08-14 and committed here instead of being re-fetched at runtime.
# Pinning matters because:
#  - a mutable "latest release" lookup, or trusting a same-release
#    checksums.txt asset, both trust whatever GitHub happens to be serving
#    for that tag right now -- a later force-push to the tag, a
#    re-uploaded asset, or a compromised maintainer/CI credential could
#    swap the bytes this addon downloads and executes with no
#    client-side signal at all;
#  - these digests instead only ever change via a reviewed code edit to
#    this table (together with SERVER_TAG), so upgrading the bundled
#    server is a deliberate, auditable decision, not an unattended fetch.
PINNED_SHA256 = {
    ("Darwin", "arm64"): "0efa6838193b8a1c7061da59e55f8548e7178e98bc92192fc89d8cebb451942d",
    ("Darwin", "x86_64"): "1f2bb5ba83f00e43ce5551d67db103013e8ad7019dddaa91c07d6c281dd6efee",
    ("Linux", "arm64"): "deb245a9ce1dd3e1090738dc86ae3bbded72acd86170ccaa76dc62d0b623a7d5",
    ("Linux", "armv7"): "20bf3f9e7f33885312629eb4436ad3e4e935c4b5393bf36fb73c3731d9437676",
    ("Linux", "x86_64"): "12ba364e1ee5ff53709a6c9ea324c94f339cf88eef50a47d08658572c84030c2",
    ("Windows", "arm64"): "dc0a4ff038fa89b70dca5c70af64208aff2e6089594f46c9f2c49ee0e7a6ac84",
    ("Windows", "x86_64"): "c5864989582784f65cde37fced5ca845d411937b0811fa4cbaebf644b98eb7f1",
}


class DownloadError(Exception):
    """Raised for any failure while fetching/installing the server binary."""


class NoAssetError(DownloadError):
    """Raised when SERVER_TAG has no pinned asset for this platform/arch."""


class UnsupportedPlatformError(DownloadError):
    """Raised when this platform cannot run a downloaded server binary at all.

    Kodi 19+ targets Android API >= 29, where Android 10+ enforces W^X:
    exec() of any file under the app's writable home directory -- where
    addon_data lives -- is blocked outright, so an installed binary could
    never be launched there regardless of which release asset matched.
    addon_data on Android also lives on emulated external storage, where
    os.chmod(0o755) is a silent no-op. Confirmed by the identical failure
    in elgatito/plugin.video.elementum#669 ([Errno 13] Permission denied).
    The only remedy is pointing Settings -> Streaming server -> Server URL
    at a server running elsewhere.
    """


def platform_key():
    """Return (os_name, arch) matching the goreleaser archive naming.

    os_name is one of {"Linux", "Darwin", "Windows", "Android"}; arch is one
    of {"x86_64", "arm64", "armv7"} (or the raw `platform.machine()` value
    when it doesn't match a known mapping). Android runs a Linux kernel, so
    platform.system() alone can't tell it apart from desktop Linux -- Kodi
    sets ANDROID_ROOT/ANDROID_STORAGE in its process environment there, and
    some Android Python builds also report "android" via sys.platform.
    """
    if _is_android():
        os_name = "Android"
    else:
        system = platform.system()
        os_name = {"Linux": "Linux", "Darwin": "Darwin", "Windows": "Windows"}.get(system, system)

    machine = (platform.machine() or "").lower()
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine in ("armv7l", "armv6l", "armv8l"):
        # armv8l: a 32-bit Android/Linux userspace running on an ARMv8
        # (aarch64) kernel reports this via platform.machine() -- the
        # normal case on Android TV sticks (e.g. Chromecast with Google
        # TV) running Kodi's armeabi-v7a APK. Treat it the same as the
        # native 32-bit ARM variants.
        arch = "armv7"
    else:
        arch = machine

    return os_name, arch


def _is_android():
    if os.environ.get("ANDROID_ROOT") or os.environ.get("ANDROID_STORAGE"):
        return True
    return "android" in sys.platform.lower()


def _asset_name(os_name, arch):
    """Return the goreleaser archive name for (os_name, arch)."""
    ext = "zip" if os_name == "Windows" else "tar.gz"
    return "stremio-server_%s_%s.%s" % (os_name, arch, ext)


def _asset_download_url(asset_name):
    """Deterministically derive the SERVER_TAG download URL for an asset."""
    return "https://github.com/%s/releases/download/%s/%s" % (
        GITHUB_REPO, SERVER_TAG, asset_name)


def select_asset(os_name, arch):
    """Return {"name", "url", "sha256"} for the pinned SERVER_TAG asset
    matching (os_name, arch), or None when this platform/arch combo has no
    pinned asset (upstream doesn't build one, or it's otherwise unsupported).
    """
    sha256 = PINNED_SHA256.get((os_name, arch))
    if sha256 is None:
        return None
    name = _asset_name(os_name, arch)
    return {"name": name, "url": _asset_download_url(name), "sha256": sha256}


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _download_to_file(url, dest_path, progress_cb):
    """Stream `url` into `dest_path`, returning the sha256 hex digest."""
    if requests is None:
        raise DownloadError('the "requests" package is required to download the server binary')
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise DownloadError("download failed: %s" % exc)

    try:
        total_size = int(resp.headers.get("Content-Length"))
    except (AttributeError, TypeError, ValueError):
        total_size = None

    sha256 = hashlib.sha256()
    done = 0
    try:
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                if not chunk:
                    continue
                fh.write(chunk)
                sha256.update(chunk)
                done += len(chunk)
                if progress_cb is not None:
                    progress_cb(done, total_size)
    except requests.RequestException as exc:
        _safe_remove(dest_path)
        raise DownloadError("download failed: %s" % exc)
    except Exception:
        # Includes a cancel signalled by progress_cb raising DownloadError.
        _safe_remove(dest_path)
        raise
    finally:
        resp.close()

    return sha256.hexdigest()


def _target_member_name(os_name):
    return BINARY_NAME + (".exe" if os_name == "Windows" else "")


def _is_safe_member(name):
    """Reject archive member paths that could escape the extraction dir."""
    if not name:
        return False
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return False
    if len(normalized) >= 2 and normalized[1] == ":":  # e.g. "C:/..."
        return False
    return not any(part == ".." for part in normalized.split("/"))


def _find_tar_member(tar, target_name):
    for info in tar.getmembers():
        if not info.isfile() or not _is_safe_member(info.name):
            continue
        if os.path.basename(info.name) == target_name:
            return info
    raise DownloadError("archive is missing the %s binary" % target_name)


def _find_zip_member(zf, target_name):
    for info in zf.infolist():
        if info.is_dir() or not _is_safe_member(info.filename):
            continue
        if os.path.basename(info.filename) == target_name:
            return info
    raise DownloadError("archive is missing the %s binary" % target_name)


def _extract_binary(archive_path, asset_name, target_name, dest_path):
    """Extract `target_name` from the downloaded archive straight to dest_path."""
    if asset_name.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            member = _find_zip_member(zf, target_name)
            with zf.open(member) as src, open(dest_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive_path, mode="r:*") as tar:
            member = _find_tar_member(tar, target_name)
            src = tar.extractfile(member)
            if src is None:
                raise DownloadError("archive is missing the %s binary" % target_name)
            with src, open(dest_path, "wb") as dst:
                shutil.copyfileobj(src, dst)


def verify_executable(path):
    """Best-effort confirmation that the installed binary can be exec()'d.

    Runs `<path> version` and treats only an OSError raised by the exec()
    attempt itself (e.g. EACCES from a noexec-mounted addon_data, or
    ENOEXEC for a binary built for the wrong architecture) as fatal. A
    non-zero exit status or a timeout is tolerated silently: some builds
    may not implement the `version` subcommand at all, and refusing an
    otherwise-successful install over that would be a worse outcome than
    skipping the check.
    """
    try:
        subprocess.run(
            [path, "version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=VERIFY_TIMEOUT)
    except OSError:
        raise UnsupportedPlatformError("%s cannot be executed on this device" % path)
    except subprocess.TimeoutExpired:
        pass


def install_binary(dest_dir, progress_cb=None):
    """Download+install the stremio-server-go binary matching this platform.

    Returns the final binary path (matching lib.service_runner.resolve_binary's
    ``<addon_data_dir>/bin/stremio-server[.exe]`` bundled-binary lookup).
    `progress_cb(done_bytes, total_bytes)` is called for every chunk written
    during the archive download (total_bytes is None when the response
    doesn't advertise a Content-Length); it may raise (e.g. DownloadError
    on user cancel) to abort the download cleanly.

    The downloaded archive's SHA-256 is checked against PINNED_SHA256 -- a
    fixed, reviewed table for SERVER_TAG committed in this repository --
    instead of a "latest release" lookup or that release's own
    checksums.txt asset: both of those are just more data GitHub happens
    to be serving right now, no more trustworthy than the download itself.
    A mismatch is refused rather than installed, guarding against a
    compromised release (hijacked CI/CD, compromised maintainer
    credentials, or a malicious fork a user was tricked into pointing at)
    shipping a backdoored binary that would otherwise install, and later
    run, with no integrity check at all.

    Raises UnsupportedPlatformError (a DownloadError subclass) immediately,
    before any network request, when running under Android: Android 10+'s
    W^X enforcement blocks exec() of anything under the app's writable
    home directory, so a downloaded binary could never run there no matter
    which asset matched. Also raises NoAssetError (a DownloadError
    subclass), before any network request, when this platform/arch has no
    pinned SERVER_TAG asset. Raises DownloadError on any other failure.
    """
    if _is_android():
        raise UnsupportedPlatformError(
            "Android blocks executing downloaded binaries (W^X); point "
            "Settings -> Streaming server -> Server URL at a server "
            "running elsewhere instead")

    os_name, arch = platform_key()
    asset = select_asset(os_name, arch)
    if asset is None:
        raise NoAssetError("no pinned %s release asset for %s/%s" % (SERVER_TAG, os_name, arch))

    asset_name = asset["name"]
    download_url = asset["url"]
    expected_sha256 = asset["sha256"]

    os.makedirs(dest_dir, exist_ok=True)
    archive_path = os.path.join(dest_dir, ".stremio-server" + PART_SUFFIX)

    try:
        digest = _download_to_file(download_url, archive_path, progress_cb)

        if digest.lower() != expected_sha256.lower():
            raise DownloadError("checksum mismatch for %s" % asset_name)

        target_name = _target_member_name(os_name)
        final_path = os.path.join(dest_dir, target_name)
        tmp_binary_path = final_path + PART_SUFFIX
        try:
            _extract_binary(archive_path, asset_name, target_name, tmp_binary_path)
            os.replace(tmp_binary_path, final_path)
        finally:
            _safe_remove(tmp_binary_path)
    finally:
        _safe_remove(archive_path)

    if os_name != "Windows":
        os.chmod(final_path, 0o755)
    verify_executable(final_path)
    return final_path
