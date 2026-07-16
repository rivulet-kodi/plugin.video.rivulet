#!/usr/bin/env python3
"""Capture a curated set of Rivulet screenshots for README.md/site/index.html.

Drives a *running* `kodi-standalone` instance over its raw TCP JSON-RPC
socket (port 9090, always on - no webserver/auth setup needed) to walk
through the addon's screens and Kodi's native Add-ons browser, taking a
screenshot at each stop via the `screenshot` input action (written by
Kodi to `debug.screenshotpath`, see `userdata/guisettings.xml`), then
resizes/redacts/renames the result into `artwork/screenshots/`.

Requirements (all one-time, on the machine actually running Kodi):
  - Kodi installed with the Rivulet addon, logged in, with at least one
    Stremio addon providing catalogs/streams (mirrors a real dev setup -
    this script does not install or configure any of that).
  - `debug.screenshotpath` set in userdata/guisettings.xml, e.g.:
        <setting id="debug.screenshotpath">/home/you/kodi-screens/</setting>
  - ImageMagick (`magick`) on PATH.

Usage:
    pkill -f kodi-standalone   # ensure a clean slate
    kodi-standalone &
    python3 scripts/capture_screenshots.py

Every step is logged; a failed/slow step (e.g. a stream never resolving)
just gets skipped rather than hanging the whole run - re-run for that one
shot by hand if needed, editing SELECTION at the bottom.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time

SHOTS_DIR = os.path.expanduser("~/kodi-screens")
RAW_DIR = "/tmp/rivulet-shots"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(_REPO_ROOT, "artwork", "screenshots")

# Box (in the raw screenshot's own pixel coordinates) covering the
# "Logged in as <email>" line on HomeWindow - blacked out before resize
# so a real account email never ends up in a committed image.
EMAIL_BOX = (300, 315, 1060, 425)


class KodiRPC:
    """Newline-agnostic JSON-RPC client for Kodi's always-on TCP socket
    (127.0.0.1:9090) - no webserver/auth setup required, unlike the HTTP
    transport."""

    def __init__(self, host="127.0.0.1", port=9090, timeout=10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""
        self._id = 0
        self._decoder = json.JSONDecoder()

    def _read_one(self):
        while True:
            text = self.buf.decode("utf-8", "ignore")
            try:
                obj, idx = self._decoder.raw_decode(text)
                self.buf = text[idx:].lstrip().encode("utf-8")
                return obj
            except json.JSONDecodeError:
                pass
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("Kodi JSON-RPC socket closed")
            self.buf += chunk

    def call(self, method, params=None):
        self._id += 1
        req = {"jsonrpc": "2.0", "method": method, "id": self._id}
        if params is not None:
            req["params"] = params
        self.sock.sendall(json.dumps(req).encode())
        while True:
            obj = self._read_one()
            if obj.get("id") == self._id:
                return obj


def wait_for_kodi(attempts=30):
    for _ in range(attempts):
        try:
            rpc = KodiRPC()
            if rpc.call("JSONRPC.Ping").get("result") == "pong":
                return rpc
        except OSError:
            pass
        time.sleep(1)
    raise SystemExit("Kodi's JSON-RPC socket never came up - is kodi-standalone running?")


def take_screenshot(rpc, name, settle=1.0):
    """Trigger TakeScreenshot, wait for the new file to land and finish
    writing, copy it into RAW_DIR/<name>.png."""
    time.sleep(settle)
    before = set(os.listdir(SHOTS_DIR))
    rpc.call("Input.ExecuteAction", {"action": "screenshot"})
    new_file = None
    deadline = time.time() + 6
    while time.time() < deadline:
        time.sleep(0.3)
        after = set(os.listdir(SHOTS_DIR)) - before
        if after:
            new_file = sorted(after)[0]
            break
    if not new_file:
        print(f"  ! no screenshot appeared for {name!r}, skipping", file=sys.stderr)
        return None
    src = os.path.join(SHOTS_DIR, new_file)
    last_size, stable_deadline = -1, time.time() + 5
    while time.time() < stable_deadline:
        size = os.path.getsize(src)
        if size > 0 and size == last_size:
            break
        last_size = size
        time.sleep(0.25)
    dst = os.path.join(RAW_DIR, f"{name}.png")
    shutil.copy(src, dst)
    return dst


def curate(raw_path, out_name, redact=False, width=1400):
    """Optionally black out the account-email box, downscale, and drop
    the result into artwork/screenshots/<out_name>.png."""
    if raw_path is None:
        return
    cmd = ["magick", raw_path]
    if redact:
        x1, y1, x2, y2 = EMAIL_BOX
        cmd += ["-fill", "black", "-draw", f"rectangle {x1},{y1} {x2},{y2}"]
    dst = os.path.join(OUT_DIR, f"{out_name}.png")
    cmd += ["-resize", f"{width}x", "-strip", "-colors", "256", dst]
    subprocess.run(cmd, check=True)


def goto_home(rpc, delay=2.5):
    """(Re)launch the addon - always lands back on HomeWindow, regardless
    of how deep the previous walk went. Cheap and idempotent, so it's the
    reliable way to reset navigation state between screens."""
    rpc.call("Addons.ExecuteAddon", {"addonid": "plugin.video.rivulet"})
    time.sleep(delay)


def main():
    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    rpc = wait_for_kodi()
    print("Kodi JSON-RPC ready.")

    goto_home(rpc, delay=3.0)
    curate(take_screenshot(rpc, "home"), "home", redact=True)

    rpc.call("Input.Select")  # Discover
    time.sleep(2.5)
    curate(take_screenshot(rpc, "discover_catalogs"), "discover-catalogs")

    rpc.call("Input.Select")  # first catalog -> coverflow
    time.sleep(3.0)
    curate(take_screenshot(rpc, "discover_coverflow"), "discover-coverflow")

    rpc.call("Input.Select")  # a title -> detail + streams
    time.sleep(2.5)
    curate(take_screenshot(rpc, "detail_streams"), "detail-streams")

    rpc.call("Input.Select")  # a stream -> resolving dialog
    time.sleep(3.0)
    curate(take_screenshot(rpc, "resolving_stream"), "resolving-stream")
    rpc.call("Input.Back")  # cancel - real resolution can take minutes/fail

    goto_home(rpc)
    rpc.call("Input.Down")  # Discover -> Search
    time.sleep(0.4)
    rpc.call("Input.Select")
    time.sleep(2.0)
    curate(take_screenshot(rpc, "search"), "search")

    rpc.call("Input.Select")  # open keyboard
    time.sleep(1.5)
    rpc.call("Input.SendText", {"text": "star wars", "done": True})
    time.sleep(8.0)
    curate(take_screenshot(rpc, "search_results"), "search-results")

    goto_home(rpc)
    rpc.call("Input.Down")
    time.sleep(0.3)
    rpc.call("Input.Down")  # Discover -> Search -> Library
    time.sleep(0.4)
    rpc.call("Input.Select")
    time.sleep(2.0)
    curate(take_screenshot(rpc, "library"), "library")

    goto_home(rpc)  # focus persists on Library from the run above
    rpc.call("Input.Down")  # Library -> Addons
    time.sleep(0.4)
    rpc.call("Input.Select")
    time.sleep(2.0)
    curate(take_screenshot(rpc, "addons_manager"), "addons-manager")

    # Exit the addon entirely, then Kodi's own native Add-ons browser.
    rpc.call("Input.Back")  # close AddonsWindow -> Home
    time.sleep(1.0)
    rpc.call("Input.Back")  # close HomeWindow -> Kodi shell
    time.sleep(1.5)
    rpc.call("GUI.ActivateWindow", {"window": "addonbrowser"})
    time.sleep(1.2)
    rpc.call("Input.Select")  # My add-ons
    time.sleep(1.2)
    for _ in range(11):  # ".." -> ... -> Video add-ons (alphabetical category list)
        rpc.call("Input.Down")
        time.sleep(0.15)
    rpc.call("Input.Select")
    time.sleep(1.2)
    rpc.call("Input.Down")  # ".." -> first video addon
    time.sleep(0.2)
    rpc.call("Input.Down")  # -> Rivulet (alphabetical: .. , Rai Play, Rivulet, ...)
    time.sleep(0.3)
    curate(take_screenshot(rpc, "kodi_video_addons"), "kodi-addon-browser")

    rpc.call("Input.Select")  # addon info dialog
    time.sleep(1.2)
    curate(take_screenshot(rpc, "kodi_addon_info"), "kodi-addon-info")

    print(f"Done. Curated screenshots in {OUT_DIR}")


if __name__ == "__main__":
    main()
