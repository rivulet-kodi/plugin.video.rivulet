# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT
# ruff: noqa: B018 - a bare attribute expression is how vulture records a use

"""Names vulture cannot see used, one per line with who uses them.

vulture only parses this file; nothing imports or runs it. It stays out
of the shipped addon: .github/workflows/build.yml and repo.yml archive
only addon.xml, default.py, service.py, resources, lib and LICENSE.
Framework callbacks Kodi or http.server call by name are listed in
pyproject.toml's `[tool.vulture] ignore_names` instead, because they recur
in many classes.
"""
from lib import procflags, service_runner
from lib.ui import libraryaction, posterwarm, views

# lib/ui/router.py's `_view_action(name)` calls `getattr(views, name)()`.
views.home  # router.py dispatch: action=home, and every unknown action
views.open_settings  # router.py dispatch: action=settings
views.sync_addons_now  # router.py dispatch: settings.xml RunPlugin(?action=sync_addons_now)

# Windows' CreateProcess reads these STARTUPINFO fields, not Python.
procflags.subprocess.STARTUPINFO.dwFlags  # read by CreateProcess on Windows
procflags.subprocess.STARTUPINFO.wShowWindow  # read by CreateProcess on Windows

# Docstring wiring: help()/pydoc read it off the namedtuple class.
service_runner.LogLevels.__doc__  # namedtuple docstring, read by help()

# Reset seams called by tests/conftest.py's autouse `_fresh_process_state`.
libraryaction.clear_library_writes  # conftest seam: pending library writes
posterwarm.reset_warmer  # conftest seam: warmer queue/thread state
