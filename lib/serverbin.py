"""Downloads and installs the stremio-server-go binary from GitHub releases.

Pure Python (no Kodi imports) so this module can be exercised directly with
plain python3. lib/service_runner.py's resolve_binary() looks for the
binary at ``<addon_data_dir>/bin/stremio-server`` (``.exe`` on Windows) --
install_binary() targets that exact location.

Reference: M0Rf30/stremio-server-go's .goreleaser.yml `archives.name_template`
for the asset-naming convention this module has to match:
    stremio-server_{Os-titlecased}_{arch}[v{goarm}].{tar.gz|zip}
e.g. stremio-server_Linux_x86_64.tar.gz, stremio-server_Windows_arm64.zip,
stremio-server_Linux_armv7.tar.gz, stremio-server_Android_arm64.tar.gz.
"""
import hashlib
import os
import platform
import shutil
import sys
import tarfile
import zipfile

try:
    import requests
except ImportError:  # pragma: no cover - exercised only without the dependency
    requests = None

GITHUB_REPO = "M0Rf30/stremio-server-go"
GITHUB_API_URL = "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO
USER_AGENT = "plugin.video.rivulet"

BINARY_NAME = "stremio-server"
CHECKSUMS_ASSET_NAME = "checksums.txt"
PART_SUFFIX = ".part"
DOWNLOAD_CHUNK_SIZE = 64 * 1024
REQUEST_TIMEOUT = 30


class DownloadError(Exception):
    """Raised for any failure while fetching/installing the server binary."""


class NoAssetError(DownloadError):
    """Raised when the latest release has no asset for this platform/arch."""


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
    elif machine in ("armv7l", "armv6l"):
        arch = "armv7"
    else:
        arch = machine

    return os_name, arch


def _is_android():
    if os.environ.get("ANDROID_ROOT") or os.environ.get("ANDROID_STORAGE"):
        return True
    return "android" in sys.platform.lower()


def latest_release():
    """GET the latest release metadata (dict with "assets", "tag_name", ...)."""
    if requests is None:
        raise DownloadError('the "requests" package is required to check for releases')
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    try:
        resp = requests.get(GITHUB_API_URL, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise DownloadError("GitHub API request failed: %s" % exc)
    try:
        return resp.json()
    except ValueError as exc:
        raise DownloadError("GitHub API returned invalid JSON: %s" % exc)


def select_asset(release, os_name, arch):
    """Return the release asset dict matching (os_name, arch), or None."""
    ext = "zip" if os_name == "Windows" else "tar.gz"
    expected = "stremio-server_%s_%s.%s" % (os_name, arch, ext)
    for asset in release.get("assets") or []:
        if asset.get("name") == expected:
            return asset
    return None


def _find_checksums_asset(release):
    for asset in release.get("assets") or []:
        if asset.get("name") == CHECKSUMS_ASSET_NAME:
            return asset
    return None


def _lookup_checksum(checksums_asset, asset_name):
    """Return the expected sha256 hex digest for `asset_name`, or None."""
    url = checksums_asset.get("browser_download_url")
    if not url or not asset_name:
        return None
    if requests is None:
        raise DownloadError('the "requests" package is required to verify checksums')
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise DownloadError("failed to fetch checksums.txt: %s" % exc)
    for line in resp.text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == asset_name:
            return parts[0]
    return None


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _download_to_file(url, dest_path, progress_cb, total_size=None):
    """Stream `url` into `dest_path`, returning the sha256 hex digest."""
    if requests is None:
        raise DownloadError('the "requests" package is required to download the server binary')
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise DownloadError("download failed: %s" % exc)

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


def install_binary(dest_dir, progress_cb=None):
    """Download+install the stremio-server-go binary matching this platform.

    Returns the final binary path (matching lib.service_runner.resolve_binary's
    ``<addon_data_dir>/bin/stremio-server[.exe]`` bundled-binary lookup).
    `progress_cb(done_bytes, total_bytes)` is called for every chunk written
    during the archive download (total_bytes is None if unknown); it may
    raise (e.g. DownloadError on user cancel) to abort the download cleanly.
    Raises DownloadError (or its NoAssetError subclass) on any failure.
    """
    os_name, arch = platform_key()
    release = latest_release()
    asset = select_asset(release, os_name, arch)
    if asset is None:
        raise NoAssetError("no release asset for %s/%s" % (os_name, arch))

    asset_name = asset.get("name") or ""
    download_url = asset.get("browser_download_url")
    if not download_url:
        raise DownloadError("release asset %r has no download URL" % asset_name)

    expected_sha256 = None
    checksums_asset = _find_checksums_asset(release)
    if checksums_asset is not None:
        expected_sha256 = _lookup_checksum(checksums_asset, asset_name)

    os.makedirs(dest_dir, exist_ok=True)
    archive_path = os.path.join(dest_dir, ".stremio-server" + PART_SUFFIX)

    try:
        digest = _download_to_file(download_url, archive_path, progress_cb, asset.get("size"))

        if expected_sha256 and digest.lower() != expected_sha256.lower():
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
    return final_path
