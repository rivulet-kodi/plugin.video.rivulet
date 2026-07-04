"""Tests for lib.serverbin (stremio-server-go binary download/install).

Reference: M0Rf30/stremio-server-go's .goreleaser.yml `archives.name_template`
and the live v0.8.5 release (asset list + checksums.txt baked into
REALISTIC_RELEASE below). No network access - `fake_requests` patches the
real `requests.get` the same way lib.serverbin's module-scope `requests`
import resolves it.
"""
import hashlib
import io
import os
import platform
import stat
import sys
import tarfile

import pytest
import requests

from lib.serverbin import (
    DownloadError,
    GITHUB_API_URL,
    GITHUB_REPO,
    NoAssetError,
    install_binary,
    latest_release,
    platform_key,
    select_asset,
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


# --- select_asset ------------------------------------------------------


def _asset(name, size):
    return {
        "name": name,
        "browser_download_url":
            "https://github.com/%s/releases/download/v0.8.5/%s" % (GITHUB_REPO, name),
        "size": size,
    }


# Real v0.8.5 asset list, verified live against the GitHub release API.
REALISTIC_RELEASE = {
    "tag_name": "v0.8.5",
    "assets": [
        _asset("checksums.txt", 805),
        _asset("stremio-server_Android_arm64.tar.gz", 8987177),
        _asset("stremio-server_Darwin_arm64.tar.gz", 8861602),
        _asset("stremio-server_Darwin_x86_64.tar.gz", 9527733),
        _asset("stremio-server_Linux_arm64.tar.gz", 5583253),
        _asset("stremio-server_Linux_armv7.tar.gz", 5193398),
        _asset("stremio-server_Linux_x86_64.tar.gz", 6827169),
        _asset("stremio-server_Windows_arm64.zip", 8537957),
        _asset("stremio-server_Windows_x86_64.zip", 9542582),
    ],
}


@pytest.mark.parametrize("os_name,arch,expected_name", [
    ("Linux", "x86_64", "stremio-server_Linux_x86_64.tar.gz"),
    ("Linux", "arm64", "stremio-server_Linux_arm64.tar.gz"),
    ("Linux", "armv7", "stremio-server_Linux_armv7.tar.gz"),
    ("Darwin", "x86_64", "stremio-server_Darwin_x86_64.tar.gz"),
    ("Darwin", "arm64", "stremio-server_Darwin_arm64.tar.gz"),
    ("Windows", "x86_64", "stremio-server_Windows_x86_64.zip"),
    ("Windows", "arm64", "stremio-server_Windows_arm64.zip"),
    ("Android", "arm64", "stremio-server_Android_arm64.tar.gz"),
])
def test_select_asset_matches_realistic_release_assets(os_name, arch, expected_name):
    asset = select_asset(REALISTIC_RELEASE, os_name, arch)
    assert asset is not None
    assert asset["name"] == expected_name


@pytest.mark.parametrize("os_name,arch", [
    ("Darwin", "armv7"),   # goreleaser ignores {goos: darwin, goarch: arm}
    ("Windows", "armv7"),  # goreleaser ignores {goos: windows, goarch: arm}
    ("Linux", "i386"),     # never built - goarch list is amd64/arm64/arm only
    ("Android", "x86_64"),  # android build id only targets arm64
])
def test_select_asset_returns_none_for_unavailable_combos(os_name, arch):
    assert select_asset(REALISTIC_RELEASE, os_name, arch) is None


def test_select_asset_returns_none_when_no_assets_key():
    assert select_asset({}, "Linux", "x86_64") is None


# --- latest_release ------------------------------------------------------


def _json_response(data, status_code=200):
    class _Resp:
        ok = status_code < 400

        def __init__(self):
            self.status_code = status_code

        def raise_for_status(self):
            if not self.ok:
                raise requests.exceptions.HTTPError("%s error" % self.status_code)

        def json(self):
            return data

    return _Resp()


def _text_response(text, status_code=200):
    class _Resp:
        ok = status_code < 400

        def __init__(self):
            self.status_code = status_code

        def raise_for_status(self):
            if not self.ok:
                raise requests.exceptions.HTTPError("%s error" % self.status_code)

    resp = _Resp()
    resp.text = text
    return resp


def _invalid_json_response():
    class _Resp:
        ok = True
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            raise ValueError("invalid json")

    return _Resp()


class _StreamResponse:
    """Stand-in for a streamed requests.Response (archive-download seam)."""

    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.ok = status_code < 400
        self.closed = False

    def raise_for_status(self):
        if not self.ok:
            raise requests.exceptions.HTTPError("%s error" % self.status_code)

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i:i + chunk_size]

    def close(self):
        self.closed = True


def test_latest_release_returns_parsed_json_from_correct_url(fake_requests):
    release = {"tag_name": "v0.8.5", "assets": []}
    fake_requests.queue_get(_json_response(release))

    result = latest_release()

    assert result == release
    assert fake_requests.calls[0]["url"] == GITHUB_API_URL


def test_latest_release_wraps_connection_error_in_download_error(fake_requests):
    fake_requests.queue_get(requests.exceptions.ConnectionError("refused"))

    with pytest.raises(DownloadError):
        latest_release()


def test_latest_release_wraps_http_error_in_download_error(fake_requests):
    fake_requests.queue_get(_json_response({}, status_code=500))

    with pytest.raises(DownloadError):
        latest_release()


def test_latest_release_wraps_invalid_json_in_download_error(fake_requests):
    fake_requests.queue_get(_invalid_json_response())

    with pytest.raises(DownloadError):
        latest_release()


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


def test_install_binary_downloads_verifies_checksum_and_installs(tmp_path, monkeypatch, fake_requests):
    _set_platform(monkeypatch, "Linux", "x86_64")
    binary_content = b"#!/bin/sh\necho fake-stremio-server\n"
    archive_bytes = _make_tar_gz({"stremio-server": binary_content})
    asset_name = "stremio-server_Linux_x86_64.tar.gz"
    correct_checksum = hashlib.sha256(archive_bytes).hexdigest()
    release = {
        "assets": [
            _asset(asset_name, len(archive_bytes)),
            _asset("checksums.txt", 0),
        ],
    }
    checksums_text = "%s  %s\n" % (correct_checksum, asset_name)

    fake_requests.queue_get(_json_response(release))
    fake_requests.queue_get(_text_response(checksums_text))
    fake_requests.queue_get(_StreamResponse(archive_bytes))

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
    assert not (tmp_path / ".stremio-server.part").exists()
    assert not os.path.exists(result_path + ".part")


def test_install_binary_finds_binary_nested_in_a_safe_subdirectory(tmp_path, monkeypatch, fake_requests):
    _set_platform(monkeypatch, "Linux", "x86_64")
    binary_content = b"nested-binary"
    archive_bytes = _make_tar_gz({"dist/stremio-server": binary_content})
    release = {"assets": [_asset("stremio-server_Linux_x86_64.tar.gz", len(archive_bytes))]}

    fake_requests.queue_get(_json_response(release))
    fake_requests.queue_get(_StreamResponse(archive_bytes))

    result_path = install_binary(str(tmp_path))

    assert os.path.isfile(result_path)
    with open(result_path, "rb") as fh:
        assert fh.read() == binary_content


def test_install_binary_checksum_mismatch_raises_download_error(tmp_path, monkeypatch, fake_requests):
    _set_platform(monkeypatch, "Linux", "x86_64")
    archive_bytes = _make_tar_gz({"stremio-server": b"binary-content"})
    asset_name = "stremio-server_Linux_x86_64.tar.gz"
    release = {
        "assets": [
            _asset(asset_name, len(archive_bytes)),
            _asset("checksums.txt", 0),
        ],
    }
    wrong_checksum_text = "%s  %s\n" % ("0" * 64, asset_name)

    fake_requests.queue_get(_json_response(release))
    fake_requests.queue_get(_text_response(wrong_checksum_text))
    fake_requests.queue_get(_StreamResponse(archive_bytes))

    with pytest.raises(DownloadError, match="checksum"):
        install_binary(str(tmp_path))

    assert not (tmp_path / ".stremio-server.part").exists()
    assert not (tmp_path / "stremio-server").exists()


def test_install_binary_rejects_path_traversal_member_names(tmp_path, monkeypatch, fake_requests):
    _set_platform(monkeypatch, "Linux", "x86_64")
    archive_bytes = _make_tar_gz({"../stremio-server": b"malicious-payload"})
    release = {"assets": [_asset("stremio-server_Linux_x86_64.tar.gz", len(archive_bytes))]}

    fake_requests.queue_get(_json_response(release))
    fake_requests.queue_get(_StreamResponse(archive_bytes))

    with pytest.raises(DownloadError, match="missing"):
        install_binary(str(tmp_path))

    assert not (tmp_path / "stremio-server").exists()
    assert not (tmp_path / ".stremio-server.part").exists()


def test_install_binary_raises_no_asset_error_when_platform_unsupported(tmp_path, monkeypatch, fake_requests):
    _set_platform(monkeypatch, "Linux", "riscv64")
    release = {"assets": [_asset("stremio-server_Linux_x86_64.tar.gz", 123)]}
    fake_requests.queue_get(_json_response(release))

    with pytest.raises(NoAssetError):
        install_binary(str(tmp_path))


def test_no_asset_error_is_a_download_error_subclass():
    assert issubclass(NoAssetError, DownloadError)


def test_install_binary_progress_cb_exception_aborts_and_cleans_up_partial_file(
        tmp_path, monkeypatch, fake_requests):
    _set_platform(monkeypatch, "Linux", "x86_64")
    archive_bytes = _make_tar_gz({"stremio-server": b"some-bytes"})
    release = {"assets": [_asset("stremio-server_Linux_x86_64.tar.gz", len(archive_bytes))]}

    fake_requests.queue_get(_json_response(release))
    fake_requests.queue_get(_StreamResponse(archive_bytes))

    def cancel(done, total):
        raise DownloadError("cancelled by user")

    with pytest.raises(DownloadError, match="cancelled"):
        install_binary(str(tmp_path), progress_cb=cancel)

    assert not (tmp_path / ".stremio-server.part").exists()
