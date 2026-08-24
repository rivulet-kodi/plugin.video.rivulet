"""Downloads and installs the stremio-server-go binary from GitHub releases.

Pure Python (no Kodi imports) so this module can be exercised directly with
plain python3. lib/service_runner.py's resolve_binary() looks for the
binary at install_dir()'s pick -- ``<addon_data_dir>/bin/stremio-server``
(``.exe`` on Windows) everywhere except Android, where addon_data lives on
a noexec mount and install_dir() instead picks one of android_bin_dirs()'s
app-private ``/data`` locations -- install_binary() targets that exact
same install_dir() location.

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
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile

try:
    import requests
except ImportError:  # pragma: no cover - exercised only without the dependency
    requests = None  # type: ignore[assignment]

from lib import procflags

GITHUB_REPO = "M0Rf30/stremio-server-go"
SERVER_TAG = "v0.12.1"
USER_AGENT = "plugin.video.rivulet"

BINARY_NAME = "stremio-server"
PART_SUFFIX = ".part"
DOWNLOAD_CHUNK_SIZE = 64 * 1024
REQUEST_TIMEOUT = 30
VERIFY_TIMEOUT = 15

#: `sys.platform` values CPython reports on Apple's mobile systems, and the
#: `platform.machine()` hardware-model prefixes real iOS/tvOS devices
#: report -- see `_apple_mobile_os()` for why both are needed.
_APPLE_MOBILE_PLATFORMS = ("ios", "ipados", "tvos")
_APPLE_MOBILE_MODEL_PREFIXES = ("iphone", "ipad", "ipod", "appletv")

#: Where install_binary() records the SERVER_TAG it installed, next to the
#: binary itself so the two cannot be separated. Dot-prefixed to keep it
#: out of the way of resolve_binary()'s lookup.
TAG_STAMP_NAME = ".server-tag"
#: Enough for any plausible tag; a bound so a corrupt/huge stamp file can
#: never be slurped whole.
TAG_STAMP_READ_LIMIT = 64
# SHA-256 digests for every stremio-server-go SERVER_TAG release asset,
# computed locally from the downloaded v0.12.1 assets on 2026-08-25 and
# cross-checked against that release's checksums.txt (they agree), then
# committed here instead of being re-fetched at runtime. Pinning matters
# because:
#  - a mutable "latest release" lookup, or trusting a same-release
#    checksums.txt asset, both trust whatever GitHub happens to be serving
#    for that tag right now -- a later force-push to the tag, a
#    re-uploaded asset, or a compromised maintainer/CI credential could
#    swap the bytes this addon downloads and executes with no
#    client-side signal at all;
#  - these digests instead only ever change via a reviewed code edit to
#    this table (together with SERVER_TAG), so upgrading the bundled
#    server is a deliberate, auditable decision, not an unattended fetch.
#
# The two Android rows arrived with v0.12.1 and are what make an
# on-device server viable at all: unlike the Linux rows (pure-Go, static)
# they are cgo builds linked against bionic, verified from the published
# artifacts as `ELF pie executable ... dynamically linked` with NEEDED
# exactly {liblog.so, libdl.so, libm.so, libc.so}. Both properties are
# load-bearing -- Android ships no `/etc/resolv.conf`, so a pure-Go
# binary's resolver falls back to `127.0.0.1:53` and every tracker/DHT
# lookup fails ("nothing resolved"), while a libc++_shared.so dependency
# (what these builds NEEDed before `-static-libstdc++`) cannot be
# satisfied by a binary with no APK to bundle it in. Re-check both
# whenever SERVER_TAG moves.
PINNED_SHA256 = {
    ("Android", "arm64"): "c9b3dae133233cdd86c6a99ccaefacd9668ace372421d3def80acb468d9dc79f",
    ("Android", "armv7"): "690a7cfcbcdba17248dd7eaa99e89147edc389999084d3916fd1b360e2f91a5e",
    ("Darwin", "arm64"): "91e7888e1fc51ee638434f104fedaf84555f97eea679895ef7ecc9e325eb2722",
    ("Darwin", "x86_64"): "40635226cd42924424c2e2484810f20e99db28df0248f16b0c9068a03edba367",
    ("Linux", "arm64"): "0bec323c13d32228a6c6bcd6ff19d88181cd5c8294628b5ec1bd0fafb8d2d636",
    ("Linux", "armv7"): "825246f90eaf27809d6f5a68ab5539df6d40b10210e6e8ed03d0b1a49a93c5ca",
    ("Linux", "x86_64"): "bf07ece88ef0cdd5dc5c6b4ad8f6537bb15b4995790e975f57302b72268ffc83",
    ("Windows", "arm64"): "68b9cf230aee7dc103b60cdaf9df3dec8a4bc4c0c8a3abc1e5a714db8da84415",
    ("Windows", "x86_64"): "04fd3598be1b9c01f4934454488b25b4f64764f11188a580951daaeb651cb870",
}


class DownloadError(Exception):
    """Raised for any failure while fetching/installing the server binary."""


class NoAssetError(DownloadError):
    """Raised when SERVER_TAG has no pinned asset for this platform/arch."""


class UnsupportedPlatformError(DownloadError):
    """Raised when this platform cannot run a downloaded server binary at all.

    Two families of platform qualify:

    - Android. NOT a single, outright ban -- exec() is denied by up to two
      independent mechanisms, and which one (if either) applies depends on
      the device:

      1. A mount-flag denial. Kodi's own addon_data (special://profile)
         lives on emulated external storage -- `/storage/emulated` mounted
         via FUSE, with `/storage` itself a `noexec` tmpfs above it -- so
         exec() of any file placed there fails with EACCES no matter its
         chmod mode or the device's SELinux policy; chmod(0o755) itself
         still works fine there, it just cannot buy back what the mount
         flag forbids. Confirmed empirically (Xiaomi `ross`, Android 14,
         SELinux Permissive): the exact pinned Linux/armv7 asset execs and
         serves correctly from `/data/local/tmp`, byte-for-byte identical
         to the copy that fails with "can't execute: Permission denied"
         from addon_data. This is why install_binary()'s caller now
         targets install_dir()'s app-private `/data` location
         (`/data/user/0/<pkg>/files/<addon_id>/bin`, `f2fs`, not `noexec`)
         instead of addon_data on Android -- moving off the noexec mount
         entirely sidesteps this mechanism.
      2. An SELinux-*enforcing* device's W^X policy. Kodi 19+ targets
         Android API >= 29, where Android 10+ additionally enforces W^X:
         exec() of any file under a location the app itself can write to
         -- which the private `/data` directory above still is -- is
         denied by SELinux when the device runs *enforcing* (not
         Permissive) and the running Kodi build's targetSdk is >= 29. This
         is the one case moving off addon_data does NOT fix: it is a
         cross-domain-exec policy check, not a mount property. Confirmed
         by the identical failure via the same private-directory strategy
         in elgatito/plugin.video.elementum's daemon.py (see
         `get_android_bin_folders()`/`ensure_exec_perms()`, its Popen
         call, and its `ctypes.cdll.LoadLibrary` fallback for exactly this
         case) -- issue #669 reports [Errno 13] Permission denied on an
         enforcing/SDK>=29 device even from its own app-private directory.

      So the attempt now succeeds wherever SELinux is Permissive (as on
      the device this was verified against) or the running Kodi build's
      targetSdk predates 29 (mechanism 2 does not apply), and fails only
      on an enforcing, targetSdk>=29 device -- there this exception still
      fires, from verify_executable()'s exec attempt, exactly as it did
      before this platform-specific pre-network refusal was removed.
    - iOS/iPadOS/tvOS. The sandbox forbids spawning arbitrary executables,
      and unsigned Mach-O cannot be loaded by dyld at all -- so even a
      correctly-built binary is unrunnable. Upstream publishes no
      ios/tvos asset either: the Darwin assets are macOS Mach-O
      (PLATFORM_MACOS), which those systems reject outright.

    In both cases the only remedy is pointing Settings -> Streaming server
    -> Server URL at a server running elsewhere.
    """


def platform_key():
    """Return (os_name, arch) matching the goreleaser archive naming.

    os_name is one of {"Linux", "Darwin", "Windows", "Android", "iOS",
    "tvOS"}; arch is one of {"x86_64", "arm64", "armv7"} (or the raw
    `platform.machine()` value when it doesn't match a known mapping).
    Neither Android nor the Apple mobile systems can be recognised from
    `platform.system()` alone -- they report "Linux" and "Darwin" exactly
    as their desktop counterparts do (see `_is_android` and
    `_apple_mobile_os` for the signals that separate them).

    Android has pinned assets of its own for armv7/arm64 as of
    SERVER_TAG v0.12.1 (cgo builds linked against bionic -- see
    PINNED_SHA256's comment for why that matters); any other Android arch
    falls back to the matching Linux row (see select_asset()), and
    iOS/tvOS have no assets at all. The os_name value still lets callers
    report what was actually detected, and lets install_binary() refuse
    iOS/tvOS before touching the network -- Android no longer gets a
    pre-network refusal; see UnsupportedPlatformError's and
    install_binary()'s docstrings for why.
    """
    if _is_android():
        os_name = "Android"
    else:
        system = platform.system()
        os_name = _apple_mobile_os() or {
            "Linux": "Linux", "Darwin": "Darwin", "Windows": "Windows"}.get(system, system)

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
    """True on Android, which reports platform.system() == "Linux" like any
    other Linux. Kodi sets ANDROID_ROOT/ANDROID_STORAGE in its process
    environment there, and some Android Python builds also report
    "android" via sys.platform."""
    if os.environ.get("ANDROID_ROOT") or os.environ.get("ANDROID_STORAGE"):
        return True
    return "android" in sys.platform.lower()


def _apple_mobile_os():
    """Return "tvOS"/"iOS" on an Apple mobile system, else None.

    Kodi's iOS/tvOS builds embed a CPython configured for Darwin, so
    `platform.system()` reports plain "Darwin" there exactly as it does on
    macOS -- indistinguishable without a second signal. Two are used:

    - `platform.machine()`. Unlike macOS, where it is the CPU
      architecture, on real iOS/tvOS hardware `utsname.machine` is the
      hardware model ("iPhone14,5", "AppleTV11,1"), which also tells iOS
      and tvOS apart. (Simulators report "arm64"/"x86_64" instead, and are
      not a Kodi target.)
    - `sys.platform`. CPython's own iOS support (3.13+) reports
      "ios"/"tvos" here, so a future Kodi built against it is covered
      without relying on the model string.
    """
    machine = (platform.machine() or "").lower()
    sys_platform = sys.platform.lower()
    if not (sys_platform in _APPLE_MOBILE_PLATFORMS
            or machine.startswith(_APPLE_MOBILE_MODEL_PREFIXES)):
        return None
    if sys_platform == "tvos" or machine.startswith("appletv"):
        return "tvOS"
    return "iOS"


_ANDROID_APP_DATA_RE = re.compile(r"/Android/data/([^/]+)/files/")


def android_bin_dirs(profile_dir, addon_id):
    """Return, in preference order, the Android app-private bin
    directories install_dir() could target instead of `profile_dir` --
    which on Android sits on a noexec-mounted filesystem (see
    UnsupportedPlatformError's docstring). Empty when not on Android, or
    when `profile_dir` doesn't match the Kodi-on-Android layout below.

    Kodi's Android process carries no KODI_*/HOME environment variable to
    read the app's own package name from -- confirmed empirically: only
    stock ANDROID_* vars (ANDROID_DATA, ANDROID_ROOT, ANDROID_STORAGE,
    EXTERNAL_STORAGE) are present -- so the package name is instead
    parsed out of `profile_dir` itself. Kodi's `special://profile` on
    Android always resolves under
    ".../Android/data/<package>/files/.kodi/userdata/...": extracting
    <package> from that segment gives the exact identifier Android uses
    to name the app's own private directories under `/data`.

    Two candidates are returned, most-likely-to-work first:
      - /data/user/0/<package>/files/<addon_id>/bin -- the per-user-profile
        path (Android 7+; user 0 is the only user on virtually every Kodi
        box).
      - /data/data/<package>/files/<addon_id>/bin -- the pre-multi-user
        alias, kept as a fallback for older Android releases.
    Both sit under the app's own uid-owned `/data` partition -- confirmed
    empirically as `f2fs`, not `noexec`, unlike `/storage/emulated` --
    namespaced by `addon_id` so other Kodi addons installing their own
    binaries here can never collide with this one.
    """
    if not _is_android():
        return []
    match = _ANDROID_APP_DATA_RE.search(profile_dir)
    if not match:
        return []
    package = match.group(1)
    return [
        os.path.join("/data/user/0", package, "files", addon_id, "bin"),
        os.path.join("/data/data", package, "files", addon_id, "bin"),
    ]


def install_dir(profile_dir, addon_id):
    """Return the directory install_binary() -- and every caller that must
    agree with it (service_runner.resolve_binary()/is_bundled_binary(),
    the `.server-tag` stamp) -- should target.

    On Android this is the first android_bin_dirs() candidate whose
    grandparent -- ".../<package>/files", the app's own private data
    root, which Android guarantees exists and is writable by the app the
    moment it is installed (confirmed empirically:
    /data/user/0/org.xbmc.kodi/files, mode 0770, owned by the app's own
    uid) -- already exists and is writable. That ancestor is checked
    rather than the candidate's immediate parent (".../<addon_id>")
    because that per-addon directory does not exist yet on a first
    install; install_binary()'s `os.makedirs(dest_dir, exist_ok=True)`
    creates it (and `bin` under it) in one step as long as `files` itself
    is writable, so `files` writability is the real precondition worth
    testing for.

    Falls back to the plain, historical `<profile_dir>/bin` -- correct on
    every non-Android platform, and also the exact location
    verify_executable()'s failure path deliberately leaves an unverified
    binary at when nothing was installed yet -- when not on Android, when
    `profile_dir` doesn't match the expected Android layout, or when
    neither Android candidate's `files` ancestor is usable.
    """
    for candidate in android_bin_dirs(profile_dir, addon_id):
        files_dir = os.path.dirname(os.path.dirname(candidate))
        if os.path.isdir(files_dir) and os.access(files_dir, os.W_OK):
            return candidate
    return os.path.join(profile_dir, "bin")


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
    asset at all (upstream doesn't build one, or it's otherwise
    unsupported).

    For os_name == "Android", an ("Android", arch) row is preferred and
    exists for armv7/arm64 as of v0.12.1. Any other Android arch (an
    x86_64 Android TV box, say -- upstream builds no Android asset for
    it) falls back to the ("Linux", arch) row instead: those binaries do
    exec() and serve correctly on Android once installed somewhere
    exec-capable (confirmed empirically: the pinned Linux/armv7 asset ran
    unmodified from an app-private `/data` directory), but they are
    pure-Go builds using Go's own DNS resolver, and Android ships no
    `/etc/resolv.conf` for it to read -- so every lookup falls back to
    `127.0.0.1:53` and fails ("nothing resolved") the moment the server
    needs to reach a tracker/DHT bootstrap host. Such a device needs
    either an upstream Android asset for its arch, pinned here, or a
    system resolv.conf provided some other way; there is no in-addon
    workaround for a pure-Go binary.
    """
    sha256 = PINNED_SHA256.get((os_name, arch))
    if sha256 is None and os_name == "Android":
        return select_asset("Linux", arch)
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
    skipping the check. On Windows this spawn is window-suppressed (see
    lib/procflags.py) so a fresh install never flashes a console box.
    """
    try:
        subprocess.run(
            [path, "version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=VERIFY_TIMEOUT,
            **procflags.no_window_kwargs())
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

    The binary is chmod'd and verify_executable()'d while still under its
    `.part` name, and only promoted over any existing one once that
    passes: an install that turns out to be unrunnable must never take
    down a binary that was already serving (see the upgrade caller in
    lib.service_runner). The one exception is documented at that check.
    A successful install records SERVER_TAG via `_write_tag_stamp` so a
    later release bump can tell this binary is out of date.

    Raises UnsupportedPlatformError (a DownloadError subclass) immediately,
    before any network request, on iOS/iPadOS/tvOS: sandboxing rules out
    exec()ing a downloaded binary there no matter where it is installed, so
    there is nothing to gain by fetching one (see that exception's
    docstring). Android has no such pre-network refusal: `dest_dir` is
    normally install_dir()'s app-private location, which usually allows
    exec() (see UnsupportedPlatformError's docstring for the two mechanisms
    that can still deny it there), so the download always proceeds and
    verify_executable() decides afterward -- on failure it re-raises this
    same exception post-network, with the archive already downloaded and
    the chmod'd binary deliberately left in place (see the
    verify_executable() call below). Also raises NoAssetError (a
    DownloadError subclass), before any network request, when this
    platform/arch has no pinned SERVER_TAG asset -- for Android only
    reachable on an arch with neither an Android nor a Linux row (see
    select_asset()). Raises DownloadError on any other failure.
    """
    apple_mobile = _apple_mobile_os()
    if apple_mobile:
        raise UnsupportedPlatformError(
            "%s sandboxing blocks executing downloaded binaries; point "
            "Settings -> Streaming server -> Server URL at a server "
            "running elsewhere instead" % apple_mobile)

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
            if os_name != "Windows":
                os.chmod(tmp_binary_path, 0o755)
            # Verify BEFORE promoting: this function is now also the
            # upgrade path (see lib.service_runner's
            # _upgrade_bundled_if_stale), so a replacement that cannot be
            # exec'd must not be allowed to clobber a binary that was
            # serving fine -- the caller's "keep the installed one"
            # fallback would otherwise be handed a path pointing at the
            # broken new file.
            try:
                verify_executable(tmp_binary_path)
            except UnsupportedPlatformError:
                # Nothing installed yet, so there is nothing to protect,
                # and leaving the (chmod'd, correctly-hashed) binary in
                # place is what lets the service's unsupported_platform
                # latch self-heal if the cause was a transient
                # noexec/EACCES condition rather than a permanent ban --
                # resolve_binary() finding it later clears the latch.
                # Unstamped on purpose: it is unverified, so the next
                # session treats it as upgradable.
                if not os.path.exists(final_path):
                    os.replace(tmp_binary_path, final_path)
                raise
            os.replace(tmp_binary_path, final_path)
        finally:
            _safe_remove(tmp_binary_path)
    finally:
        _safe_remove(archive_path)

    _write_tag_stamp(dest_dir)
    return final_path


def installed_tag(dest_dir):
    """Return the SERVER_TAG the binary currently in `dest_dir` was
    installed from, or None when unknown.

    None covers every case callers must treat identically -- "not the
    current tag": no stamp file (installed before stamping existed, the
    write failed, or the install was promoted unverified), an unreadable
    one, one that is not decodable text, and an empty one.
    """
    try:
        with open(os.path.join(dest_dir, TAG_STAMP_NAME)) as fh:
            return fh.read(TAG_STAMP_READ_LIMIT).strip() or None
    except (OSError, UnicodeError):
        return None


def _write_tag_stamp(dest_dir):
    """Record SERVER_TAG next to the binary just installed into `dest_dir`.

    Best-effort on purpose: the binary is already installed and verified
    by the time this runs, so failing the whole install over a stamp
    write would turn a working server into a user-visible error. A
    missing stamp only costs `installed_tag()` reporting None, which
    callers treat as "not the current tag" -- i.e. one redundant
    reinstall attempt, not a broken install.
    """
    try:
        with open(os.path.join(dest_dir, TAG_STAMP_NAME), "w") as fh:
            fh.write(SERVER_TAG)
    except OSError:
        pass
