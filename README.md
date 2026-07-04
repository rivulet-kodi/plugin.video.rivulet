# Rivulet

A Kodi video addon that reimplements the [Stremio](https://www.stremio.com/)
client experience: Discover/Search catalogs from the Stremio addon protocol,
addon management (install from manifest URL), meta/stream resolution, and
playback through a [stremio-server-go](https://github.com/M0Rf30/stremio-server-go)
streaming server. Optional login syncs your addon collection with your
Stremio account.

This addon does not host, index or provide any media content — it is a
client for third-party Stremio addons that you install yourself.

## Install

1. Grab `plugin.video.rivulet-<version>.zip` from a
   [release](https://github.com/M0Rf30/plugin.video.rivulet/releases) or from
   the "Build addon zip" GitHub Actions artifact.
2. In Kodi: **Settings → Add-ons → Install from zip file** and pick the
   downloaded zip.
3. Open **Rivulet** from the Videos section of the home screen.

## Streaming server

All playback is resolved through a streaming server speaking the same
`enginefs` HTTP API as Stremio's own `server.js`
([stremio-server-go](https://github.com/M0Rf30/stremio-server-go) is a
pure-Go, drop-in implementation of it). Configure it under
**Settings → Streaming server**:

- **Server URL** — where the streaming server is listening. Defaults to
  `http://127.0.0.1:11470`, the standard Stremio server port. Point this at
  any already-running `stremio-server-go` (or official Stremio server)
  instance, local or remote.
- **Run embedded server** — when enabled, the addon's background service
  spawns and supervises a local `stremio-server-go` process for the lifetime
  of the Kodi session, and stops it on shutdown.
- **Server binary path** — the `stremio-server-go` executable to run when the
  embedded server is enabled. Leave empty to auto-detect: the service first
  looks in `special://profile/addon_data/plugin.video.rivulet/bin/`, then on
  `PATH`.

As of this release, `stremio-server-go` no longer needs to be installed by
hand: use the **Download stremio-server binary** button under
**Settings → Streaming server** to fetch the correct build for your platform
straight from the
[stremio-server-go releases](https://github.com/M0Rf30/stremio-server-go/releases)
into the addon's `bin/` folder. Subtitles are pulled from your installed
Stremio subtitle addons at playback time — OpenSubtitles v3 is preinstalled —
and sorted using the **Preferred subtitle language** setting under
**Settings → Subtitles**.

## Development

`lib/stremio/` and `lib/store.py` are plain Python with no `xbmc*` imports,
so they run and test outside Kodi. Everything Kodi-specific lives in
`lib/ui/`, `lib/service_runner.py`, `default.py` and `service.py`.

```sh
pip install requests pytest
pytest tests/
```
