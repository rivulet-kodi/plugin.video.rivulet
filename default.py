# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Plugin entry point for plugin.video.rivulet.

Kodi invokes this script directly (not as an import), passing the
plugin:// invocation as sys.argv. Bootstrap sys.path so the addon's own
``lib`` package is importable, then hand off to ``lib.ui.entry`` (which
opens Rivulet's Home window for a bare launch and the router for every
other action). The body lives there so it is importable under test.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.ui import entry

entry.run(sys.argv)
