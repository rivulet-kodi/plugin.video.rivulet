"""Background service entry point for plugin.video.rivulet.

Kodi launches this script once at startup (xbmc.service extension).
Bootstrap sys.path so the addon's own ``lib`` package is importable,
then hand off to the service runner, which supervises the local
stremio-server-go binary for the lifetime of the Kodi session.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.service_runner import main

main()
