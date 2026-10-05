#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Live smoke test for a running Stream4Me bridge. Run before every release.

The bridge leans on Stream4Me internals (channel `search()`/`episodios()`/
`findvideos()`, `core.servertools`, server modules' globals) that are not an
API and change whenever Stream4Me updates itself from GitHub -- which it does
on every Kodi start. A break there fails silently as empty results, so unit
tests with fakes cannot catch it. This asks a real bridge for a handful of
well-known titles and checks that:

  - each request answers inside Rivulet's 15s AddonClient timeout;
  - at least one title per type returns streams;
  - every returned url actually loads when fetched the way Kodi would (with
    Kodi's own User-Agent unless the stream carries proxyHeaders), following
    redirects -- so a /play/<key> mega link is exercised too.

Usage (bridge on this machine, or `adb forward tcp:11480 tcp:11480` first):

    python3 tools/s4me_smoke.py [--port 11480]

Exit status 0 = pass. Needs the network; never run it from the unit suite.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

#: Well-known titles with long-lived Italian availability. Movies first.
TITLES = (
    ("movie", "tt0111161"),   # Le ali della liberta
    ("movie", "tt0068646"),   # Il padrino
    ("movie", "tt1375666"),   # Inception
    ("series", "tt0903747:1:1"),  # Breaking Bad
    ("series", "tt0944947:1:1"),  # Il Trono di Spade
)
#: What Kodi's player sends when a stream has no headers of its own.
KODI_UA = "Kodi/21.2 (X11; Linux x86_64) App_Bitness/64 Version/21.2"
CLIENT_TIMEOUT = 15.0


def _get_json(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def _playable(stream):
    url = stream.get("url") or ""
    if url.startswith("magnet:"):
        return True, "magnet (not fetched)"
    headers = {"User-Agent": KODI_UA, "Range": "bytes=0-1023"}
    hints = stream.get("behaviorHints") or {}
    headers.update(((hints.get("proxyHeaders") or {}).get("request")) or {})
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read(1024)
            return resp.status in (200, 206), "HTTP %d" % resp.status
    except urllib.error.HTTPError as exc:
        return False, "HTTP %d" % exc.code
    except Exception as exc:  # noqa: BLE001 - report, never crash the run
        return False, repr(exc)[:80]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--port", type=int, default=11480)
    args = parser.parse_args()
    base = "http://127.0.0.1:%d" % args.port

    failures = []
    try:
        manifest = _get_json(base + "/manifest.json", 5)
    except Exception as exc:  # noqa: BLE001
        print("FAIL bridge not answering on %s: %r" % (base, exc))
        return 1
    print("bridge %s %s" % (manifest.get("id"), manifest.get("version")))

    hits = {"movie": 0, "series": 0}
    for ctype, sid in TITLES:
        started = time.monotonic()
        try:
            streams = _get_json("%s/stream/%s/%s.json" % (base, ctype, sid), 60)["streams"]
        except Exception as exc:  # noqa: BLE001
            failures.append("%s %s: request failed %r" % (ctype, sid, exc))
            continue
        elapsed = time.monotonic() - started
        print("%-6s %-14s %5.1fs %d stream(s)" % (ctype, sid, elapsed, len(streams)))
        if elapsed > CLIENT_TIMEOUT:
            failures.append("%s %s took %.1fs (> %.0fs client timeout)"
                            % (ctype, sid, elapsed, CLIENT_TIMEOUT))
        if streams:
            hits[ctype] += 1
        for stream in streams:
            ok, detail = _playable(stream)
            print("    %s %-28s %-22s %s" % (
                "ok  " if ok else "FAIL", stream.get("name", "")[:28],
                (stream.get("title") or "")[:22], detail))
            if not ok:
                failures.append("%s %s: %s unplayable (%s)"
                                % (ctype, sid, stream.get("name"), detail))
    for ctype, count in hits.items():
        if not count:
            failures.append("no %s title returned any stream" % ctype)

    if failures:
        print("\n%d failure(s):" % len(failures))
        for line in failures:
            print("  - " + line)
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
