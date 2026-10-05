# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Kodi-facing UI layer for plugin.video.rivulet.

Everything importing xbmc*/xbmcgui/xbmcplugin/xbmcvfs lives under this
package (plus lib/service_runner.py and the two entry-point scripts).
lib/stremio/* and lib/store.py stay pure Python and must never be
imported the other way around.
"""
