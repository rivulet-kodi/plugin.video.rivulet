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


# --- PINNED_SHA256 / select_asset -------------------------------------------

# Exact digests reviewed against the v0.12.0 GitHub release page +
# checksums.txt. Any drift here (wrong tag, tampered digest, added/removed
# platform) must be a deliberate, reviewed edit to lib/serverbin.py.
EXPECTED_PINNED_SHA256 = {
    ("Darwin", "arm64"): "0efa6838193b8a1c7061da59e55f8548e7178e98bc92192fc89d8cebb451942d",
    ("Darwin", "x86_64"): "1f2bb5ba83f00e43ce5551d67db103013e8ad7019dddaa91c07d6c281dd6efee",
    ("Linux", "arm64"): "deb245a9ce1dd3e1090738dc86ae3bbded72acd86170ccaa76dc62d0b623a7d5",
    ("Linux", "armv7"): "20bf3f9e7f33885312629eb4436ad3e4e935c4b5393bf36fb73c3731d9437676",
    ("Linux", "x86_64"): "12ba364e1ee5ff53709a6c9ea324c94f339cf88eef50a47d08658572c84030c2",
    ("Windows", "arm64"): "dc0a4ff038fa89b70dca5c70af64208aff2e6089594f46c9f2c49ee0e7a6ac84",
    ("Windows", "x86_64"): "c5864989582784f65cde37fced5ca845d411937b0811fa4cbaebf644b98eb7f1",
}


def test_server_tag_is_pinned_to_v0_12_0():
    assert SERVER_TAG == "v0.12.0"


def test_pinned_sha256_table_matches_reviewed_v0_12_0_digests_exactly():
    assert PINNED_SHA256 == EXPECTED_PINNED_SHA256


@pytest.mark.parametrize("os_name,arch,expected_name", [
    ("Linux", "x86_64", "stremio-server_Linux_x86_64.tar.gz"),
    ("Linux", "arm64", "stremio-server_Linux_arm64.tar.gz"),
    ("Linux", "armv7", "stremio-server_Linux_armv7.tar.gz"),
    ("Darwin", "x86_64", "stremio-server_Darwin_x86_64.tar.gz"),
    ("Darwin", "arm64", "stremio-server_Darwin_arm64.tar.gz"),
    ("Windows", "x86_64", "stremio-server_Windows_x86_64.zip"),
    ("Windows", "arm64", "stremio-server_Windows_arm64.zip"),
])
def test_select_asset_returns_deterministic_name_url_and_pinned_digest(
        os_name, arch, expected_name):
    asset = select_asset(os_name, arch)
    assert asset is not None
    assert asset["name"] == expected_name
    assert asset["url"] == (
        "https://github.com/%s/releases/download/v0.12.0/%s" % (GITHUB_REPO, expected_name))
    assert asset["sha256"] == EXPECTED_PINNED_SHA256[(os_name, arch)]


@pytest.mark.parametrize("os_name,arch", [
    ("Darwin", "armv7"),    # goreleaser ignores {goos: darwin, goarch: arm}
    ("Windows", "armv7"),   # goreleaser ignores {goos: windows, goarch: arm}
    ("Linux", "i386"),      # never built - goarch list is amd64/arm64/arm only
    ("Android", "arm64"),   # Android has no pinned asset at all
    ("Android", "x86_64"),
])
def test_select_asset_returns_none_for_unpinned_combos(os_name, arch):
    assert select_asset(os_name, arch) is None


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
        "https://github.com/%s/releases/download/v0.12.0/stremio-server_Linux_x86_64.tar.gz"
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


def test_install_binary_raises_unsupported_platform_error_on_android_with_no_http_request(
        tmp_path, monkeypatch, fake_requests):
    _set_platform(monkeypatch, "Linux", "arm64", android_root="/system")

    with pytest.raises(UnsupportedPlatformError):
        install_binary(str(tmp_path))

    assert fake_requests.calls == []


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
