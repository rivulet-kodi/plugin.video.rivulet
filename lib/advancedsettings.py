"""Installs the addon's recommended advancedsettings.xml template into the
user's Kodi userdata directory.

Pure Python (no Kodi imports) so this module can be exercised directly with
plain python3, same rationale as lib/serverbin.py. install() is opt-in and
strictly non-destructive: it only ever writes when the destination does not
already exist, so it never clobbers a user's own customized
advancedsettings.xml (a single file Kodi core, other addons and the user
may all want a say in) - callers surface the two outcomes ('installed' vs.
'exists') as distinct notifications instead of silently merging or
overwriting.
"""
import os
import shutil

#: install() copied source_path -> dest_path.
STATUS_INSTALLED = 'installed'
#: install() left dest_path untouched because it already existed.
STATUS_EXISTS = 'exists'


class AdvancedSettingsError(Exception):
    """Raised when install()/read_recommended_xml() hits an I/O error."""


def install(source_path, dest_path):
    """Copy `source_path` -> `dest_path`, creating dest's parent directories
    as needed, UNLESS `dest_path` already exists.

    Returns STATUS_EXISTS without touching `dest_path` if it's already
    there (non-destructive: an existing advancedsettings.xml might be the
    user's own tuning, or another addon's), otherwise copies the file and
    returns STATUS_INSTALLED. Raises AdvancedSettingsError wrapping the
    original OSError for any other I/O failure (missing/unreadable source,
    unwritable dest directory, ...).
    """
    if os.path.exists(dest_path):
        return STATUS_EXISTS

    try:
        dest_dir = os.path.dirname(dest_path)
        if dest_dir:
            os.makedirs(dest_dir, exist_ok=True)
        shutil.copyfile(source_path, dest_path)
    except OSError as exc:
        raise AdvancedSettingsError('failed to install advancedsettings.xml: %s' % exc) from exc

    return STATUS_INSTALLED


def read_recommended_xml(source_path):
    """Return the recommended advancedsettings.xml contents as text.

    Useful for callers that want to preview/log the template without
    installing it. Raises AdvancedSettingsError on any read failure.
    """
    try:
        with open(source_path, encoding='utf-8') as fh:
            return fh.read()
    except OSError as exc:
        raise AdvancedSettingsError(
            'failed to read advancedsettings.xml template: %s' % exc
        ) from exc
