"""Tests for lib.serverbin (stremio-server-go binary download/install).

Asset names/URLs are derived deterministically from GITHUB_REPO/SERVER_TAG;
expected SHA-256 digests come from the PINNED_SHA256 table committed in
lib/serverbin.py, not from a "latest release" API call or a same-release
checksums.txt asset. `fake_requests` patches the real `requests.get` the
same way lib.serverbin's module-scope `requests` import resolves it --
only the archive download itself ever issues a request.
"""
import hashlib
import io
import os
import platform
import stat
import subprocess
import sys
import tarfile

import pytest

from lib import serverbin
from lib.serverbin import (
    GITHUB_REPO,
    PINNED_SHA256,
    SERVER_TAG,
    DownloadError,
    NoAssetError,
    UnsupportedPlatformError,
    install_binary,
    platform_key,
    select_asset,
    verify_executable,
)


def _set_platform(monkeypatch, system, machine, sys_platform="linux",
                   android_root=None, android_storage=None):
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setattr(sys, "platform", sys_platform)
    if android_root is None:
        monkeypatch.delenv("ANDROID_ROOT", raising=False)
    else:
        monkeypatch.setenv("ANDROID_ROOT", android_root)
    if android_storage is None:
        monkeypatch.delenv("ANDROID_STORAGE", raising=False)
    else:
        monkeypatch.setenv("ANDROID_STORAGE", android_storage)


# --- platform_key ----------------------------------------------------------


def test_platform_key_linux_x86_64(monkeypatch):
    _set_platform(monkeypatch, "Linux", "x86_64")
    assert platform_key() == ("Linux", "x86_64")


def test_platform_key_linux_amd64_alias_maps_to_x86_64(monkeypatch):
    _set_platform(monkeypatch, "Linux", "amd64")
    assert platform_key() == ("Linux", "x86_64")


def test_platform_key_linux_aarch64_maps_to_arm64(monkeypatch):
    _set_platform(monkeypatch, "Linux", "aarch64")
    assert platform_key() == ("Linux", "arm64")


def test_platform_key_linux_armv7l_maps_to_armv7(monkeypatch):
    _set_platform(monkeypatch, "Linux", "armv7l")
    assert platform_key() == ("Linux", "armv7")


def test_platform_key_linux_armv6l_maps_to_armv7(monkeypatch):
    _set_platform(monkeypatch, "Linux", "armv6l")
    assert platform_key() == ("Linux", "armv7")


def test_platform_key_linux_armv8l_maps_to_armv7(monkeypatch):
    _set_platform(monkeypatch, "Linux", "armv8l")
    assert platform_key() == ("Linux", "armv7")


def test_platform_key_android_armv8l_maps_to_armv7(monkeypatch):
    _set_platform(monkeypatch, "Linux", "armv8l", android_root="/system")
    assert platform_key() == ("Android", "armv7")


def test_platform_key_unknown_arch_falls_back_to_raw_lowercased_value(monkeypatch):
    _set_platform(monkeypatch, "Linux", "RISCV64")
    assert platform_key() == ("Linux", "riscv64")


def test_platform_key_android_via_android_root_env(monkeypatch):
    _set_platform(monkeypatch, "Linux", "aarch64", android_root="/system")
    assert platform_key() == ("Android", "arm64")


def test_platform_key_android_via_android_storage_env(monkeypatch):
    _set_platform(monkeypatch, "Linux", "aarch64", android_storage="/storage/emulated/0")
    assert platform_key() == ("Android", "arm64")


def test_platform_key_android_via_sys_platform(monkeypatch):
    _set_platform(monkeypatch, "Linux", "aarch64", sys_platform="android")
    assert platform_key() == ("Android", "arm64")


def test_platform_key_windows_amd64(monkeypatch):
    _set_platform(monkeypatch, "Windows", "AMD64", sys_platform="win32")
    assert platform_key() == ("Windows", "x86_64")


def test_platform_key_windows_arm64(monkeypatch):
    _set_platform(monkeypatch, "Windows", "ARM64", sys_platform="win32")
    assert platform_key() == ("Windows", "arm64")


def test_platform_key_darwin_x86_64(monkeypatch):
    _set_platform(monkeypatch, "Darwin", "x86_64", sys_platform="darwin")
    assert platform_key() == ("Darwin", "x86_64")


def test_platform_key_darwin_arm64(monkeypatch):
    _set_platform(monkeypatch, "Darwin", "arm64", sys_platform="darwin")
    assert platform_key() == ("Darwin", "arm64")


@pytest.mark.parametrize("machine,expected_os", [
    ("iPhone14,5", "iOS"),
    ("iPad13,4", "iOS"),
    ("iPod9,1", "iOS"),
    ("AppleTV11,1", "tvOS"),
])
def test_platform_key_apple_mobile_detected_from_hardware_model(
        monkeypatch, machine, expected_os):
    """Kodi's iOS/tvOS builds report platform.system() == "Darwin" exactly
    like macOS; on real hardware `platform.machine()` is the device model,
    which is what tells them apart (and tvOS from iOS)."""
    _set_platform(monkeypatch, "Darwin", machine, sys_platform="darwin")
    assert platform_key()[0] == expected_os


@pytest.mark.parametrize("sys_platform,expected_os", [
    ("ios", "iOS"),
    ("ipados", "iOS"),
    ("tvos", "tvOS"),
])
def test_platform_key_apple_mobile_detected_from_sys_platform(
        monkeypatch, sys_platform, expected_os):
    """CPython's own iOS support (3.13+) reports "ios"/"tvos" in
    sys.platform and "arm64" in platform.machine() -- no model string to
    key off, so the sys.platform signal must stand on its own."""
    _set_platform(monkeypatch, "Darwin", "arm64", sys_platform=sys_platform)
    assert platform_key() == (expected_os, "arm64")


def test_platform_key_macos_arm64_is_not_mistaken_for_apple_mobile(monkeypatch):
    """The Apple-mobile probe must not swallow desktop macOS, which is a
    fully supported target with pinned assets."""
    _set_platform(monkeypatch, "Darwin", "arm64", sys_platform="darwin")
    assert platform_key() == ("Darwin", "arm64")


# --- PINNED_SHA256 / select_asset -------------------------------------------

# Exact digests reviewed against the v0.12.1 GitHub release: computed
# locally from the downloaded assets and cross-checked against that
# release's checksums.txt. Any drift here (wrong tag, tampered digest,
# added/removed platform) must be a deliberate, reviewed edit to
# lib/serverbin.py.
EXPECTED_PINNED_SHA256 = {
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


def test_server_tag_is_pinned_to_v0_12_1():
    assert SERVER_TAG == "v0.12.1"


def test_pinned_sha256_table_matches_reviewed_v0_12_1_digests_exactly():
    assert PINNED_SHA256 == EXPECTED_PINNED_SHA256


@pytest.mark.parametrize("os_name,arch,expected_name", [
    ("Linux", "x86_64", "stremio-server_Linux_x86_64.tar.gz"),
    ("Linux", "arm64", "stremio-server_Linux_arm64.tar.gz"),
    ("Linux", "armv7", "stremio-server_Linux_armv7.tar.gz"),
    ("Darwin", "x86_64", "stremio-server_Darwin_x86_64.tar.gz"),
    ("Darwin", "arm64", "stremio-server_Darwin_arm64.tar.gz"),
    ("Windows", "x86_64", "stremio-server_Windows_x86_64.zip"),
    ("Windows", "arm64", "stremio-server_Windows_arm64.zip"),
    ("Android", "armv7", "stremio-server_Android_armv7.tar.gz"),
    ("Android", "arm64", "stremio-server_Android_arm64.tar.gz"),
])
def test_select_asset_returns_deterministic_name_url_and_pinned_digest(
        os_name, arch, expected_name):
    asset = select_asset(os_name, arch)
    assert asset is not None
    assert asset["name"] == expected_name
    assert asset["url"] == (
        "https://github.com/%s/releases/download/v0.12.1/%s" % (GITHUB_REPO, expected_name))
    assert asset["sha256"] == EXPECTED_PINNED_SHA256[(os_name, arch)]


@pytest.mark.parametrize("os_name,arch", [
    ("Darwin", "armv7"),    # goreleaser ignores {goos: darwin, goarch: arm}
    ("Windows", "armv7"),   # goreleaser ignores {goos: windows, goarch: arm}
    ("Linux", "i386"),      # never built - goarch list is amd64/arm64/arm only
    ("Android", "i386"),    # no pinned Android row, and no Linux/i386 fallback either
])
def test_select_asset_returns_none_for_unpinned_combos(os_name, arch):
    assert select_asset(os_name, arch) is None


def test_select_asset_falls_back_to_matching_linux_row_for_android():
    """An Android arch upstream builds no asset for -- x86_64, as on an
    Intel Android TV box -- must fall back to the ("Linux", arch) row
    rather than refusing: that Linux binary is confirmed (empirically) to
    exec() and serve correctly on Android once installed somewhere
    exec-capable. It is pure-Go, so its DNS is broken there (see
    select_asset()'s docstring), but a server that runs is still strictly
    better than no server at all."""
    assert ("Android", "x86_64") not in PINNED_SHA256
    assert select_asset("Android", "x86_64") == select_asset("Linux", "x86_64")


def test_select_asset_prefers_the_pinned_android_row_over_the_linux_fallback():
    """The Android (CGO/NDK, bionic-linked) rows pinned as of v0.12.1 must
    win over the same-arch Linux fallback -- that preference is the whole
    point of pinning them, since only those builds resolve DNS on
    Android."""
    for arch in ("armv7", "arm64"):
        asset = select_asset("Android", arch)
        assert asset["name"] == "stremio-server_Android_%s.tar.gz" % arch
        assert asset["sha256"] == EXPECTED_PINNED_SHA256[("Android", arch)]
        assert asset != select_asset("Linux", arch)


# --- install_binary --------------------------------------------------------


def _make_tar_gz(members):
    """members: {arcname: bytes} -> gzip-compressed tar archive bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arcname, data in members.items():
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _StreamResponse:
    """Stand-in for a streamed requests.Response (archive-download seam)."""

    def __init__(self, data, headers=None):
        self._data = data
        self.ok = True
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i:i + chunk_size]

    def close(self):
        self.closed = True


def test_install_binary_downloads_verifies_pinned_checksum_and_installs(
        tmp_path, monkeypatch, fake_requests):
    _set_platform(monkeypatch, "Linux", "x86_64")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: None)
    binary_content = b"#!/bin/sh\necho fake-stremio-server\n"
    archive_bytes = _make_tar_gz({"stremio-server": binary_content})
    correct_checksum = hashlib.sha256(archive_bytes).hexdigest()
    monkeypatch.setitem(PINNED_SHA256, ("Linux", "x86_64"), correct_checksum)

    fake_requests.queue_get(
        _StreamResponse(archive_bytes, headers={"Content-Length": str(len(archive_bytes))}))

    progress_calls = []
    result_path = install_binary(
        str(tmp_path), progress_cb=lambda done, total: progress_calls.append((done, total)))

    assert result_path == str(tmp_path / "stremio-server")
    assert os.path.isfile(result_path)
    with open(result_path, "rb") as fh:
        assert fh.read() == binary_content
    assert stat.S_IMODE(os.stat(result_path).st_mode) == 0o755
    assert progress_calls
    assert progress_calls[-1][1] == len(archive_bytes)
    assert len(fake_requests.calls) == 1
    assert fake_requests.calls[0]["url"] == (
        "https://github.com/%s/releases/download/v0.12.1/stremio-server_Linux_x86_64.tar.gz"
        % GITHUB_REPO)
    assert not (tmp_path / ".stremio-server.part").exists()
    assert not os.path.exists(result_path + ".part")


def test_install_binary_finds_binary_nested_in_a_safe_subdirectory(
        tmp_path, monkeypatch, fake_requests):
    _set_platform(monkeypatch, "Linux", "x86_64")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: None)
    binary_content = b"nested-binary"
    archive_bytes = _make_tar_gz({"dist/stremio-server": binary_content})
    correct_checksum = hashlib.sha256(archive_bytes).hexdigest()
    monkeypatch.setitem(PINNED_SHA256, ("Linux", "x86_64"), correct_checksum)

    fake_requests.queue_get(_StreamResponse(archive_bytes))

    result_path = install_binary(str(tmp_path))

    assert os.path.isfile(result_path)
    with open(result_path, "rb") as fh:
        assert fh.read() == binary_content


def test_install_binary_checksum_mismatch_raises_download_error_and_cleans_up(
        tmp_path, monkeypatch, fake_requests):
    """The archive is downloaded, but its digest doesn't match the pinned
    value -- refuse to install and leave no partial files behind."""
    _set_platform(monkeypatch, "Linux", "x86_64")
    archive_bytes = _make_tar_gz({"stremio-server": b"binary-content"})
    monkeypatch.setitem(PINNED_SHA256, ("Linux", "x86_64"), "0" * 64)

    fake_requests.queue_get(_StreamResponse(archive_bytes))

    with pytest.raises(DownloadError, match="checksum mismatch"):
        install_binary(str(tmp_path))

    assert not (tmp_path / ".stremio-server.part").exists()
    assert not (tmp_path / "stremio-server").exists()


def test_install_binary_rejects_path_traversal_member_names(tmp_path, monkeypatch, fake_requests):
    _set_platform(monkeypatch, "Linux", "x86_64")
    archive_bytes = _make_tar_gz({"../stremio-server": b"malicious-payload"})
    correct_checksum = hashlib.sha256(archive_bytes).hexdigest()
    monkeypatch.setitem(PINNED_SHA256, ("Linux", "x86_64"), correct_checksum)

    fake_requests.queue_get(_StreamResponse(archive_bytes))

    with pytest.raises(DownloadError, match="missing"):
        install_binary(str(tmp_path))

    assert not (tmp_path / "stremio-server").exists()
    assert not (tmp_path / ".stremio-server.part").exists()


def test_install_binary_raises_no_asset_error_for_unpinned_platform_before_any_network_request(
        tmp_path, monkeypatch, fake_requests):
    """A platform/arch with no PINNED_SHA256 entry must be refused locally
    -- no mutable metadata lookup, no network request of any kind."""
    _set_platform(monkeypatch, "Linux", "riscv64")

    with pytest.raises(NoAssetError):
        install_binary(str(tmp_path))

    assert fake_requests.calls == []


@pytest.mark.parametrize("machine,sys_platform", [
    ("iPhone14,5", "darwin"),   # real iOS hardware: model in machine()
    ("AppleTV11,1", "darwin"),  # real tvOS hardware
    ("arm64", "ios"),           # CPython 3.13+ iOS build
    ("arm64", "tvos"),
])
def test_install_binary_refuses_apple_mobile_before_any_network_request(
        tmp_path, monkeypatch, fake_requests, machine, sys_platform):
    """iOS/tvOS sandboxing makes a downloaded binary unrunnable, so the
    fetch must be refused locally -- notably including the case where
    platform.machine() says "arm64" and the Darwin/arm64 asset would
    otherwise match and download in full before failing at exec time."""
    _set_platform(monkeypatch, "Darwin", machine, sys_platform=sys_platform)
    # A dest_dir that does not exist yet: the refusal must land before
    # install_binary()'s os.makedirs(), so nothing at all is set up. A
    # regression that only moved the guard below the download would still
    # leave no final binary behind, and pass a weaker assertion.
    dest_dir = tmp_path / "bin"

    with pytest.raises(UnsupportedPlatformError):
        install_binary(str(dest_dir))

    assert fake_requests.calls == []
    assert not dest_dir.exists()


def test_install_binary_still_installs_on_macos_arm64(tmp_path, monkeypatch, fake_requests):
    """Guard against the Apple-mobile refusal above over-reaching into
    desktop macOS, whose Darwin/arm64 asset is pinned and supported."""
    _set_platform(monkeypatch, "Darwin", "arm64", sys_platform="darwin")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: None)
    archive_bytes = _make_tar_gz({"stremio-server": b"macos-binary"})
    monkeypatch.setitem(
        PINNED_SHA256, ("Darwin", "arm64"), hashlib.sha256(archive_bytes).hexdigest())
    fake_requests.queue_get(_StreamResponse(archive_bytes))

    result_path = install_binary(str(tmp_path))

    assert result_path == str(tmp_path / "stremio-server")
    assert fake_requests.calls[0]["url"].endswith("stremio-server_Darwin_arm64.tar.gz")


def test_no_asset_error_is_a_download_error_subclass():
    assert issubclass(NoAssetError, DownloadError)


def test_install_binary_progress_cb_exception_aborts_and_cleans_up_partial_file(
        tmp_path, monkeypatch, fake_requests):
    _set_platform(monkeypatch, "Linux", "x86_64")
    archive_bytes = _make_tar_gz({"stremio-server": b"some-bytes"})
    correct_checksum = hashlib.sha256(archive_bytes).hexdigest()
    monkeypatch.setitem(PINNED_SHA256, ("Linux", "x86_64"), correct_checksum)

    fake_requests.queue_get(_StreamResponse(archive_bytes))

    def cancel(done, total):
        raise DownloadError("cancelled by user")

    with pytest.raises(DownloadError, match="cancelled"):
        install_binary(str(tmp_path), progress_cb=cancel)

    assert not (tmp_path / ".stremio-server.part").exists()


# --- UnsupportedPlatformError / Android gating ------------------------------


def test_unsupported_platform_error_is_a_download_error_subclass():
    assert issubclass(UnsupportedPlatformError, DownloadError)


def test_unsupported_platform_error_is_not_a_no_asset_error_subclass():
    assert not issubclass(UnsupportedPlatformError, NoAssetError)


def test_install_binary_on_android_downloads_and_installs_instead_of_refusing(
        tmp_path, monkeypatch, fake_requests):
    """Android no longer refuses before touching the network: a real exec
    attempt (verify_executable(), faked here via subprocess.run) is what
    decides now -- see UnsupportedPlatformError's docstring for the two
    mechanisms that can still make that attempt fail on some devices."""
    _set_platform(monkeypatch, "Linux", "arm64", android_root="/system")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: None)
    archive_bytes = _make_tar_gz({"stremio-server": b"binary"})
    monkeypatch.setitem(
        PINNED_SHA256, ("Android", "arm64"), hashlib.sha256(archive_bytes).hexdigest())
    fake_requests.queue_get(_StreamResponse(archive_bytes))

    result_path = install_binary(str(tmp_path))

    assert result_path == str(tmp_path / "stremio-server")
    assert os.path.isfile(result_path)
    assert len(fake_requests.calls) == 1
    # The archive downloaded is the pinned Android (cgo/bionic) asset, not
    # the pure-Go Linux row select_asset() only falls back to for an arch
    # upstream builds no Android binary for.
    assert fake_requests.calls[0]["url"].endswith("stremio-server_Android_arm64.tar.gz")


def test_install_binary_on_android_raises_unsupported_platform_error_when_exec_fails(
        tmp_path, monkeypatch, fake_requests):
    """On an enforcing-SELinux/SDK>=29 device (or a noexec fallback
    install_dir), the real exec attempt fails post-network -- install_binary()
    must still raise UnsupportedPlatformError and, since nothing was
    installed yet, deliberately leave the chmod'd binary in place (see
    install_binary()'s own docstring on the upgrade-safety exception)."""
    _set_platform(monkeypatch, "Linux", "arm64", android_root="/system")

    def _raise(*args, **kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(subprocess, "run", _raise)
    archive_bytes = _make_tar_gz({"stremio-server": b"binary"})
    monkeypatch.setitem(
        PINNED_SHA256, ("Android", "arm64"), hashlib.sha256(archive_bytes).hexdigest())
    fake_requests.queue_get(_StreamResponse(archive_bytes))

    with pytest.raises(UnsupportedPlatformError):
        install_binary(str(tmp_path))

    assert len(fake_requests.calls) == 1
    final_path = tmp_path / "stremio-server"
    assert final_path.exists()
    assert stat.S_IMODE(os.stat(str(final_path)).st_mode) == 0o755
    assert serverbin.installed_tag(str(tmp_path)) is None


# --- verify_executable -------------------------------------------------


def test_verify_executable_raises_unsupported_platform_error_on_os_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise OSError("Exec format error")

    monkeypatch.setattr(subprocess, "run", _raise)

    with pytest.raises(UnsupportedPlatformError):
        verify_executable("/fake/path/stremio-server")


def test_verify_executable_tolerates_nonzero_exit_status(monkeypatch):
    class _Completed:
        returncode = 1

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Completed())

    verify_executable("/fake/path/stremio-server")  # must not raise


def test_verify_executable_tolerates_timeout(monkeypatch):
    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="stremio-server", timeout=15)

    monkeypatch.setattr(subprocess, "run", _timeout)

    verify_executable("/fake/path/stremio-server")  # must not raise


class _FakeStartupInfo:
    def __init__(self):
        self.dwFlags = 0
        self.wShowWindow = None


def test_verify_executable_forwards_no_window_kwargs_on_windows_only(monkeypatch):
    """Issue #30's second, easy-to-miss spawn site: right after an install,
    verify_executable() runs `<path> version` as a one-shot sanity check --
    on Windows that flash-pops its own empty cmd console unless it too
    forwards lib.procflags.no_window_kwargs() to subprocess.run(). Captures
    the kwargs actually reaching subprocess.run() on POSIX and on a
    simulated Windows, proving the Windows-only kwargs appear only on 'nt'
    while every argument verify_executable itself controls (the argv,
    stdout/stderr redirection, check, timeout) is unchanged either way."""
    calls = []

    class _Completed:
        returncode = 0

    def _record(*args, **kwargs):
        calls.append((args, kwargs))
        return _Completed()

    monkeypatch.setattr(serverbin.subprocess, "run", _record)

    # Pin the POSIX branch rather than inheriting the host's os.name, so this
    # half keeps testing POSIX behaviour when the suite runs ON Windows.
    monkeypatch.setattr(serverbin.procflags.os, "name", "posix")
    verify_executable("/fake/path/stremio-server")
    posix_args, posix_kwargs = calls[0]
    assert "creationflags" not in posix_kwargs
    assert "startupinfo" not in posix_kwargs

    monkeypatch.setattr(serverbin.procflags.os, "name", "nt")
    monkeypatch.setattr(
        serverbin.procflags.subprocess, "STARTUPINFO", _FakeStartupInfo, raising=False)

    verify_executable("/fake/path/stremio-server")
    win_args, win_kwargs = calls[1]

    assert win_args == posix_args == (["/fake/path/stremio-server", "version"],)
    assert win_kwargs["stdout"] == posix_kwargs["stdout"] == subprocess.DEVNULL
    assert win_kwargs["stderr"] == posix_kwargs["stderr"] == subprocess.DEVNULL
    assert win_kwargs["check"] == posix_kwargs["check"] is False
    assert win_kwargs["timeout"] == posix_kwargs["timeout"]
    assert win_kwargs["creationflags"] == 0x08000000  # CREATE_NO_WINDOW
    assert win_kwargs["startupinfo"].wShowWindow == 0  # SW_HIDE


# --- SERVER_TAG stamp ------------------------------------------------------


def _install_ok(tmp_path, monkeypatch, fake_requests):
    """Drive one successful install_binary() into `tmp_path`."""
    _set_platform(monkeypatch, "Linux", "x86_64")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: None)
    archive_bytes = _make_tar_gz({"stremio-server": b"binary"})
    monkeypatch.setitem(
        PINNED_SHA256, ("Linux", "x86_64"), hashlib.sha256(archive_bytes).hexdigest())
    fake_requests.queue_get(_StreamResponse(archive_bytes))
    return install_binary(str(tmp_path))


def test_install_binary_stamps_the_server_tag_it_installed(tmp_path, monkeypatch, fake_requests):
    """Without the stamp there is no way to tell a binary installed under
    an older SERVER_TAG from a current one, so a tag bump could never be
    delivered to an existing install."""
    _install_ok(tmp_path, monkeypatch, fake_requests)

    assert (tmp_path / serverbin.TAG_STAMP_NAME).read_text() == SERVER_TAG
    assert serverbin.installed_tag(str(tmp_path)) == SERVER_TAG


def test_installed_tag_is_none_when_never_installed(tmp_path):
    assert serverbin.installed_tag(str(tmp_path)) is None


@pytest.mark.parametrize("contents", ["", "   \n"])
def test_installed_tag_is_none_for_an_empty_stamp(tmp_path, contents):
    (tmp_path / serverbin.TAG_STAMP_NAME).write_text(contents)
    assert serverbin.installed_tag(str(tmp_path)) is None


def test_installed_tag_strips_surrounding_whitespace(tmp_path):
    (tmp_path / serverbin.TAG_STAMP_NAME).write_text("v0.9.0\n")
    assert serverbin.installed_tag(str(tmp_path)) == "v0.9.0"


def test_installed_tag_is_none_when_the_stamp_cannot_be_read(tmp_path):
    """A directory where the stamp file should be stands in for any OSError
    out of the read -- unknown must degrade to None, never propagate."""
    (tmp_path / serverbin.TAG_STAMP_NAME).mkdir()
    assert serverbin.installed_tag(str(tmp_path)) is None


def test_installed_tag_is_none_for_undecodable_stamp_bytes(tmp_path):
    """A truncated/garbage stamp raises UnicodeDecodeError, not OSError --
    and it is read from the service's supervision loop, which must not die
    over it."""
    (tmp_path / serverbin.TAG_STAMP_NAME).write_bytes(b"\xff\xfe\x00v0.9")
    assert serverbin.installed_tag(str(tmp_path)) is None


def test_install_binary_keeps_a_working_binary_when_the_new_one_fails_verification(
        tmp_path, monkeypatch, fake_requests):
    """The upgrade path reinstalls over a binary that is currently serving,
    so a replacement that cannot be exec'd must not clobber it -- the
    caller's "keep the installed one" fallback would otherwise hand back a
    path pointing at the broken new file."""
    existing = tmp_path / "stremio-server"
    existing.write_bytes(b"the-binary-that-works")
    (tmp_path / serverbin.TAG_STAMP_NAME).write_text("v0.9.0")

    _set_platform(monkeypatch, "Linux", "x86_64")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("ENOEXEC")))
    archive_bytes = _make_tar_gz({"stremio-server": b"the-broken-new-binary"})
    monkeypatch.setitem(
        PINNED_SHA256, ("Linux", "x86_64"), hashlib.sha256(archive_bytes).hexdigest())
    fake_requests.queue_get(_StreamResponse(archive_bytes))

    with pytest.raises(UnsupportedPlatformError):
        install_binary(str(tmp_path))

    assert existing.read_bytes() == b"the-binary-that-works"
    assert not (tmp_path / "stremio-server.part").exists()
    # The stamp still describes what is actually installed.
    assert serverbin.installed_tag(str(tmp_path)) == "v0.9.0"


def test_install_binary_promotes_an_unverifiable_binary_when_nothing_was_installed(
        tmp_path, monkeypatch, fake_requests):
    """With nothing to protect there is nothing to lose, and leaving the
    binary in place is what lets lib.service_runner's unsupported_platform
    latch self-heal when the cause was a transient noexec/EACCES mount
    rather than a permanent ban. It stays unstamped, so the next session
    treats it as upgradable."""
    _set_platform(monkeypatch, "Linux", "x86_64")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("EACCES")))
    archive_bytes = _make_tar_gz({"stremio-server": b"unverifiable"})
    monkeypatch.setitem(
        PINNED_SHA256, ("Linux", "x86_64"), hashlib.sha256(archive_bytes).hexdigest())
    fake_requests.queue_get(_StreamResponse(archive_bytes))

    with pytest.raises(UnsupportedPlatformError):
        install_binary(str(tmp_path))

    assert (tmp_path / "stremio-server").read_bytes() == b"unverifiable"
    assert serverbin.installed_tag(str(tmp_path)) is None


def test_install_binary_succeeds_even_when_the_stamp_cannot_be_written(
        tmp_path, monkeypatch, fake_requests):
    """The binary is installed and verified before the stamp is written --
    losing the stamp must cost at most a redundant reinstall later, never
    fail an otherwise-working install."""
    (tmp_path / serverbin.TAG_STAMP_NAME).mkdir()

    result_path = _install_ok(tmp_path, monkeypatch, fake_requests)

    assert os.path.isfile(result_path)
    assert serverbin.installed_tag(str(tmp_path)) is None


def test_install_binary_does_not_stamp_a_refused_download(tmp_path, monkeypatch, fake_requests):
    """A checksum mismatch installs nothing, so it must not leave a stamp
    claiming SERVER_TAG is installed."""
    _set_platform(monkeypatch, "Linux", "x86_64")
    monkeypatch.setitem(PINNED_SHA256, ("Linux", "x86_64"), "0" * 64)
    fake_requests.queue_get(_StreamResponse(_make_tar_gz({"stremio-server": b"binary"})))

    with pytest.raises(DownloadError):
        install_binary(str(tmp_path))

    assert serverbin.installed_tag(str(tmp_path)) is None


# --- android_bin_dirs / install_dir -----------------------------------------


def _android_profile_dir(pkg="org.xbmc.kodi", addon_id="plugin.video.rivulet"):
    """A realistic Kodi-on-Android `special://profile/addon_data/<addon_id>`
    path: `.../Android/data/<pkg>/files/.kodi/userdata/addon_data/<addon_id>/`."""
    return (
        "/storage/emulated/0/Android/data/%s/files/.kodi/userdata/"
        "addon_data/%s/" % (pkg, addon_id)
    )


def test_android_bin_dirs_derives_candidates_from_realistic_kodi_profile_path(monkeypatch):
    _set_platform(monkeypatch, "Linux", "armv8l", android_root="/system")
    assert serverbin.android_bin_dirs(_android_profile_dir(), "plugin.video.rivulet") == [
        "/data/user/0/org.xbmc.kodi/files/plugin.video.rivulet/bin",
        "/data/data/org.xbmc.kodi/files/plugin.video.rivulet/bin",
    ]


def test_android_bin_dirs_empty_when_not_android(monkeypatch):
    _set_platform(monkeypatch, "Linux", "x86_64")
    assert serverbin.android_bin_dirs(_android_profile_dir(), "plugin.video.rivulet") == []


def test_android_bin_dirs_empty_when_profile_dir_does_not_match_android_layout(monkeypatch):
    _set_platform(monkeypatch, "Linux", "armv8l", android_root="/system")
    assert serverbin.android_bin_dirs(
        "/home/user/.kodi/userdata/addon_data/plugin.video.rivulet/", "plugin.video.rivulet") == []


def test_install_dir_returns_plain_bin_when_not_android(monkeypatch):
    _set_platform(monkeypatch, "Linux", "x86_64")
    profile_dir = "/home/user/.kodi/userdata/addon_data/plugin.video.rivulet"
    assert serverbin.install_dir(profile_dir, "plugin.video.rivulet") == os.path.join(profile_dir, "bin")


def test_install_dir_returns_plain_bin_when_profile_dir_does_not_match_android_layout(monkeypatch):
    _set_platform(monkeypatch, "Linux", "armv8l", android_root="/system")
    profile_dir = "/home/user/.kodi/userdata/addon_data/plugin.video.rivulet"
    assert serverbin.install_dir(profile_dir, "plugin.video.rivulet") == os.path.join(profile_dir, "bin")


def test_install_dir_picks_first_writable_android_candidate(monkeypatch):
    _set_platform(monkeypatch, "Linux", "armv8l", android_root="/system")
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    monkeypatch.setattr(os, "access", lambda p, mode: True)
    assert serverbin.install_dir(_android_profile_dir(), "plugin.video.rivulet") == (
        "/data/user/0/org.xbmc.kodi/files/plugin.video.rivulet/bin")


def test_install_dir_falls_through_to_second_candidate_when_first_unwritable(monkeypatch):
    """The /data/user/0 candidate's `files` ancestor exists but is not
    writable (e.g. a locked-down OEM layout) -- install_dir() must fall
    through to the /data/data candidate instead of giving up."""
    _set_platform(monkeypatch, "Linux", "armv8l", android_root="/system")
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    monkeypatch.setattr(os, "access", lambda p, mode: "/data/data/" in p)
    assert serverbin.install_dir(_android_profile_dir(), "plugin.video.rivulet") == (
        "/data/data/org.xbmc.kodi/files/plugin.video.rivulet/bin")


def test_install_dir_falls_back_to_plain_bin_when_no_android_candidate_usable(monkeypatch):
    _set_platform(monkeypatch, "Linux", "armv8l", android_root="/system")
    monkeypatch.setattr(os.path, "isdir", lambda p: False)
    profile_dir = _android_profile_dir()
    assert serverbin.install_dir(profile_dir, "plugin.video.rivulet") == os.path.join(profile_dir, "bin")
