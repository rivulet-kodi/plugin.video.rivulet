"""Tests for lib.advancedsettings (opt-in advancedsettings.xml installer).

Pure Python (no Kodi imports), same rationale as lib/serverbin.py /
tests/test_serverbin.py: install()/read_recommended_xml() only ever touch
paths handed to them explicitly, so every test drives real files under
pytest's tmp_path - no fixture, no filesystem access outside tmp_path, no
network.
"""
import os
import shutil

import pytest

from lib.advancedsettings import (
    STATUS_EXISTS,
    STATUS_INSTALLED,
    AdvancedSettingsError,
    install,
    read_recommended_xml,
)

_TEMPLATE_TEXT = (
    '<advancedsettings><network><curlclienttimeout>60</curlclienttimeout></network></advancedsettings>'
)

# The actual template this addon ships - resolved relative to this test
# file (never cwd), so the integration test below works regardless of the
# directory pytest is invoked from.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED_TEMPLATE_PATH = os.path.join(_REPO_ROOT, 'resources', 'advancedsettings.xml')


# ---------------------------------------------------------------------------
# status constants
# ---------------------------------------------------------------------------


def test_status_constants_are_the_literal_strings_callers_compare_against():
    # router.py (and any other caller) is entitled to compare against the
    # literal 'installed'/'exists' strings directly, per the module's
    # documented contract - not just the named constants.
    assert STATUS_INSTALLED == 'installed'
    assert STATUS_EXISTS == 'exists'


# ---------------------------------------------------------------------------
# install(): happy path
# ---------------------------------------------------------------------------


def test_install_copies_source_to_dest_and_returns_installed(tmp_path):
    source = tmp_path / 'source.xml'
    source.write_text(_TEMPLATE_TEXT, encoding='utf-8')
    dest = tmp_path / 'dest.xml'

    status = install(str(source), str(dest))

    assert status == STATUS_INSTALLED
    assert dest.read_text(encoding='utf-8') == _TEMPLATE_TEXT


def test_install_creates_missing_parent_directories(tmp_path):
    source = tmp_path / 'source.xml'
    source.write_text(_TEMPLATE_TEXT, encoding='utf-8')
    dest = tmp_path / 'userdata' / 'nested' / 'advancedsettings.xml'
    assert not dest.parent.exists()

    status = install(str(source), str(dest))

    assert status == STATUS_INSTALLED
    assert dest.read_text(encoding='utf-8') == _TEMPLATE_TEXT


def test_install_dest_without_directory_component_still_works(tmp_path, monkeypatch):
    """dest_path may be a bare filename (empty os.path.dirname()) - must
    not crash trying to os.makedirs('')."""
    monkeypatch.chdir(tmp_path)
    with open('source.xml', 'w', encoding='utf-8') as fh:
        fh.write(_TEMPLATE_TEXT)

    status = install('source.xml', 'dest.xml')

    assert status == STATUS_INSTALLED
    with open('dest.xml', encoding='utf-8') as fh:
        assert fh.read() == _TEMPLATE_TEXT


# ---------------------------------------------------------------------------
# install(): non-destructive refusal
# ---------------------------------------------------------------------------


def test_install_refuses_and_leaves_existing_dest_untouched(tmp_path):
    source = tmp_path / 'source.xml'
    source.write_text(_TEMPLATE_TEXT, encoding='utf-8')
    dest = tmp_path / 'dest.xml'
    dest.write_text('<advancedsettings><!-- user customized --></advancedsettings>', encoding='utf-8')

    status = install(str(source), str(dest))

    assert status == STATUS_EXISTS
    # Non-destructive: the pre-existing file must come back byte-for-byte
    # unchanged - never merged or overwritten with the recommended template.
    assert dest.read_text(encoding='utf-8') == '<advancedsettings><!-- user customized --></advancedsettings>'


def test_install_refuses_even_when_dest_is_a_directory(tmp_path):
    """os.path.exists() is true for directories too; install() must still
    treat that as 'exists' rather than raising or clobbering it - the
    contract is "was something already there", not "was it a file"."""
    source = tmp_path / 'source.xml'
    source.write_text(_TEMPLATE_TEXT, encoding='utf-8')
    dest_dir = tmp_path / 'dest.xml'
    dest_dir.mkdir()

    status = install(str(source), str(dest_dir))

    assert status == STATUS_EXISTS
    assert dest_dir.is_dir()


# ---------------------------------------------------------------------------
# install(): error path
# ---------------------------------------------------------------------------


def test_install_raises_advancedsettingserror_when_source_is_missing(tmp_path):
    source = tmp_path / 'does-not-exist.xml'
    dest = tmp_path / 'dest.xml'

    with pytest.raises(AdvancedSettingsError):
        install(str(source), str(dest))

    assert not dest.exists()


def test_install_error_chains_the_original_oserror(tmp_path):
    source = tmp_path / 'does-not-exist.xml'
    dest = tmp_path / 'dest.xml'

    with pytest.raises(AdvancedSettingsError) as excinfo:
        install(str(source), str(dest))

    assert isinstance(excinfo.value.__cause__, OSError)


def test_install_wraps_copy_failure_raised_after_parent_dirs_exist(tmp_path, monkeypatch):
    """A failure inside shutil.copyfile itself (not just a missing source)
    must also come back as AdvancedSettingsError, not a raw OSError."""
    source = tmp_path / 'source.xml'
    source.write_text(_TEMPLATE_TEXT, encoding='utf-8')
    dest = tmp_path / 'dest.xml'

    def _boom(_src, _dst):
        raise PermissionError('denied')

    monkeypatch.setattr(shutil, 'copyfile', _boom)

    with pytest.raises(AdvancedSettingsError):
        install(str(source), str(dest))

    assert not dest.exists()


# ---------------------------------------------------------------------------
# read_recommended_xml()
# ---------------------------------------------------------------------------


def test_read_recommended_xml_returns_file_contents(tmp_path):
    source = tmp_path / 'source.xml'
    source.write_text(_TEMPLATE_TEXT, encoding='utf-8')

    assert read_recommended_xml(str(source)) == _TEMPLATE_TEXT


def test_read_recommended_xml_raises_advancedsettingserror_when_missing(tmp_path):
    with pytest.raises(AdvancedSettingsError):
        read_recommended_xml(str(tmp_path / 'missing.xml'))


# ---------------------------------------------------------------------------
# integration with the actual shipped template
# ---------------------------------------------------------------------------


def test_shipped_template_installs_and_is_valid_xml_with_expected_tunables(tmp_path):
    """Exercises install() against the REAL resources/advancedsettings.xml
    this addon ships (not a synthetic fixture), guarding against the
    template and the installer silently drifting apart."""
    import xml.etree.ElementTree as ET

    dest = tmp_path / 'advancedsettings.xml'

    status = install(SHIPPED_TEMPLATE_PATH, str(dest))

    assert status == STATUS_INSTALLED
    root = ET.parse(str(dest)).getroot()
    assert root.tag == 'advancedsettings'
    assert root.findtext('network/curlclienttimeout') == '60'
    assert root.findtext('network/curllowspeedtime') == '60'
    assert root.findtext('network/curlretries') == '2'
    assert root.findtext('cache/buffermode') == '1'
    assert root.findtext('cache/memorysize') == '209715200'
    assert root.findtext('cache/readfactor') == '20'
