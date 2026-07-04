"""Plugin entry point for plugin.video.rivulet.

Kodi invokes this script directly (not as an import), passing the
plugin:// invocation as sys.argv. Bootstrap sys.path so the addon's
own ``lib`` package is importable, then hand off to the router.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.ui.router import run

run()
