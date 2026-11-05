# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

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
import tempfile

#: install() copied source_path -> dest_path.
STATUS_INSTALLED = 'installed'
#: install() left dest_path untouched because it already existed.
STATUS_EXISTS = 'exists'


class AdvancedSettingsError(Exception):
    """Raised when install() hits an I/O error."""


def install(source_path, dest_path, shadow_paths=()):
    """Copy `source_path` -> `dest_path`, creating dest's parent directories
    as needed, UNLESS `dest_path` or any of `shadow_paths` already exists.

    `shadow_paths` are files Kodi reads INSTEAD of `dest_path` when they
    exist: Kodi loads the active profile's own advancedsettings.xml in
    place of the master profile's (ProfileManager::GetUserDataItem), so
    installing into the master profile beside one would report success
    for a file this profile never reads.

    Returns STATUS_EXISTS without touching `dest_path` if it's already
    there (non-destructive: an existing advancedsettings.xml might be the
    user's own tuning, or another addon's), otherwise copies the file and
    returns STATUS_INSTALLED. The copy itself is atomic: it lands in a
    fsynced temporary file in dest's own directory first, then a single
    hard link publishes it (see _publish(): unlike os.replace() it cannot
    clobber a file that appeared after the existence check).
    advancedsettings.xml is parsed by
    Kodi core itself at startup, not by this addon, and this addon's
    target hardware (Raspberry Pi / Android TV boxes running off SD card
    or eMMC) treats sudden power loss as routine rather than exceptional
    - a plain, non-atomic copy left half-written by one of those would
    make Kodi silently discard the whole file's tuning. Worse, because
    install() refuses to touch an existing `dest_path`, that truncated
    file would satisfy os.path.exists() forever after, so no later re-run
    could ever repair it. The temporary file is chmod'ed to 0644 before
    the swap: tempfile.mkstemp() creates 0600, but Rivulet's own release
    notes tell users to hand-edit this file, which on LibreELEC/OSMC
    typically means over Samba or SFTP as a different account than the
    one Kodi runs as - 0600 would silently break exactly the workflow
    the notes prescribe. Raises AdvancedSettingsError wrapping the
    original OSError for any other I/O failure (missing/unreadable
    source, unwritable dest directory, ...); the temporary file is
    removed before the error propagates, so a failed install never
    litters the destination directory.
    """
    if any(os.path.exists(path) for path in (dest_path,) + tuple(shadow_paths)):
        return STATUS_EXISTS

    try:
        dest_dir = os.path.dirname(dest_path)
        if dest_dir:
            os.makedirs(dest_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix='.tmp-', dir=dest_dir or '.')
        try:
            os.close(fd)
            shutil.copyfile(source_path, tmp_path)
            # mkstemp() is 0600 by design; the plain copyfile() this
            # replaced produced 0666 & ~umask (0644 for the default 022).
            os.chmod(tmp_path, 0o644)
            # fsync before publishing: on ext4 and SD/eMMC the rename can
            # reach the disk before the data, and the zero-length file a
            # power loss then leaves would satisfy os.path.exists() above
            # forever. 'r+b' because Windows' fsync needs a writable handle.
            with open(tmp_path, 'r+b') as fh:
                os.fsync(fh.fileno())
            installed = _publish(tmp_path, dest_path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise AdvancedSettingsError('failed to install advancedsettings.xml: %s' % exc) from exc

    return STATUS_INSTALLED if installed else STATUS_EXISTS


def _publish(tmp_path, dest_path):
    """Move `tmp_path` to `dest_path` without ever replacing an existing
    `dest_path`; return whether it was installed (the temp file is gone
    either way).

    The os.path.exists() check in install() is only a fast path: the user
    (or Kodi, or another addon) can create advancedsettings.xml between
    that check and the swap, and os.replace() would silently overwrite it.
    os.link() refuses an existing destination atomically, so it closes the
    window. Filesystems without hard links (FAT/exFAT, some network mounts)
    fall back to check-then-os.replace(), which keeps the narrow window
    there rather than failing the install.
    """
    try:
        os.link(tmp_path, dest_path)
    except FileExistsError:
        os.remove(tmp_path)
        return False
    except (OSError, NotImplementedError, AttributeError):
        if os.path.exists(dest_path):
            os.remove(tmp_path)
            return False
        os.replace(tmp_path, dest_path)
        return True
    os.remove(tmp_path)
    return True
