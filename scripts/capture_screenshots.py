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

# Box covering HomeWindow's "Logged in as <email>" status pill, blacked
# out before resize so a real account email never ends up in a committed
# image. Expressed as fractions of the screenshot's own width/height, not
# absolute pixels: the raw capture's size follows whatever resolution
# Kodi is running at, and the skin moved this pill from the left of the
# header (pre-0.9.0) to the top right when the windows were reauthored at
# 1920x1080 - an absolute box silently stops covering anything after a
# layout change, which fails open and leaks the address.
EMAIL_BOX_FRACTIONS = (0.55, 0.08, 0.95, 0.15)


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


def _raw_size(path):
    """Return the raw screenshot's (width, height) via ImageMagick."""
    out = subprocess.run(
        ["magick", "identify", "-format", "%w %h", path],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    return int(out[0]), int(out[1])


def optimise(path):
    """Shrink `path` losslessly, in place.

    These 11 images ship inside every addon zip and are served on every
    page load of the project site, so the bytes are worth reclaiming.
    Both tools are optional - a machine without them still produces
    correct, just larger, output.
    """
    if shutil.which("optipng"):
        subprocess.run(["optipng", "-quiet", "-o5", path], check=False)
    if shutil.which("zopflipng"):
        # zopflipng cannot rewrite in place; -y overwrites the temp copy.
        tmp = path + ".zopfli"
        if subprocess.run(["zopflipng", "-y", path, tmp], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            if os.path.getsize(tmp) < os.path.getsize(path):
                os.replace(tmp, path)
            else:
                os.remove(tmp)


def curate(raw_path, out_name, redact=False, width=1400):
    """Optionally black out the account-email box, downscale, optimise,
    and drop the result into artwork/screenshots/<out_name>.png."""
    if raw_path is None:
        return
    cmd = ["magick", raw_path]
    if redact:
        raw_w, raw_h = _raw_size(raw_path)
        fx1, fy1, fx2, fy2 = EMAIL_BOX_FRACTIONS
        x1, x2 = int(raw_w * fx1), int(raw_w * fx2)
        y1, y2 = int(raw_h * fy1), int(raw_h * fy2)
        cmd += ["-fill", "black", "-draw", f"rectangle {x1},{y1} {x2},{y2}"]
    dst = os.path.join(OUT_DIR, f"{out_name}.png")
    cmd += ["-resize", f"{width}x", "-strip", "-colors", "256", dst]
    subprocess.run(cmd, check=True)
    optimise(dst)


#: Kodi reports a distinct window id per screen over JSON-RPC. Its labels
#: for the addon's own dialogs are meaningless (Kodi maps custom ids onto
#: its own table, so 13001 reads as "Immediate HDD spindown"), but the ids
#: themselves are stable and are what makes this walk deterministic.
WINDOW_KODI_HOME = 10000
WINDOW_RIVULET_HOME = 13000


def current_window(rpc):
    result = rpc.call("GUI.GetProperties", {"properties": ["currentwindow"]}) or {}
    return ((result.get("result") or {}).get("currentwindow") or {}).get("id")


def goto_home(rpc, delay=2.0):
    """Return to Rivulet's HomeWindow, verified rather than assumed.

    `Addons.ExecuteAddon` re-enters the addon but does NOT dismiss what is
    already open, and every Rivulet screen is a WindowXMLDialog, so Kodi
    keeps routing input to whichever is topmost. Backing out a fixed
    number of times is not enough either - the walk used to drift a screen
    deeper on each leg, and one run typed the search query into Kodi's own
    Server URL field. So: back out until Kodi's own home is actually on
    screen, then relaunch and wait until the addon's Home actually is.
    """
    # Stop playback inside the loop, not only before it: backing out of a
    # screen can start a stream (a stuck "Preparing stream" dialog swallows
    # Back until it resolves or is cancelled), and a fixed pre-loop stop
    # cannot see one that appears afterwards. 0.5s per press was also too
    # brisk for a dialog mid-resolve - one run exhausted its budget and
    # aborted the whole capture.
    for _ in range(30):
        if current_window(rpc) == WINDOW_KODI_HOME:
            break
        for player in (rpc.call("Player.GetActivePlayers") or {}).get("result") or []:
            rpc.call("Player.Stop", {"playerid": player["playerid"]})
            time.sleep(1.5)
        rpc.call("Input.Back")
        time.sleep(0.8)
    else:
        raise SystemExit(
            "could not get back to Kodi's home screen (stuck at window %s)"
            % current_window(rpc))
    rpc.call("Addons.ExecuteAddon", {"addonid": "plugin.video.rivulet"})
    # Generous: a cold launch refreshes addon manifests over the network
    # before HomeWindow opens, and 12s was not always enough.
    for _ in range(60):
        time.sleep(0.5)
        if current_window(rpc) == WINDOW_RIVULET_HOME:
            break
    else:
        raise SystemExit(
            "Rivulet's HomeWindow never came up (stuck at window %s) - if the addon"
            " was already deep in its own window stack, restart Kodi for a clean slate"
            % current_window(rpc))
    # Opening the addon fires a "Logged in as <email>" toast. It is not
    # covered by EMAIL_BOX_FRACTIONS (that only masks HomeWindow's status
    # pill) and it WILL be captured by any screenshot taken too soon after
    # a relaunch - one run leaked an address into search-results.png. Wait
    # it out rather than trying to mask it.
    time.sleep(max(delay, 6.0))


def _home_rows():
    """Map each HomeWindow action to its row index, read out of
    `lib.ui.homewindow._MENU` - the authoritative definition.

    Parsed with `ast` rather than imported: `lib.ui.homewindow` binds
    `xbmcgui` at module scope and cannot load outside Kodi. These used to
    be hardcoded, and went stale the moment the `other` row was inserted
    at index 3 - every leg after it then walked to the wrong screen, which
    is the same class of failure as counting keypresses.

    Assumes every row is present, which holds on the capture machine: it
    is logged in (Library) and has addons publishing types outside the
    three curated rows (Other). A machine missing either would shift the
    rows below it. `continue` is conditional the same way - it needs the
    setting on and an in-band local playback-progress entry - and is
    absent on a fresh capture machine, shifting every row after it.
    """
    import ast

    source = os.path.join(_REPO_ROOT, 'lib', 'ui', 'homewindow.py')
    with open(source, encoding='utf-8') as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, 'id', None) == '_MENU' for t in node.targets):
            actions = [e.elts[1].value for e in node.value.elts]
            return {action: index for index, action in enumerate(actions)}
    raise SystemExit('could not find _MENU in %s' % source)


#: HomeWindow's rows, by action name -> row index.
HOME_ROWS = _home_rows()
HOME_MOVIES = HOME_ROWS['movies']
HOME_SEARCH = HOME_ROWS['search']
HOME_LIBRARY = HOME_ROWS['library']
HOME_ADDONS = HOME_ROWS['addons']

#: Rows to step down in Kodi's "Video add-ons" folder to reach Rivulet.
#: The folder lists ".." first, then user video plugins sorted by name;
#: on this machine that is ".." / Rai Play / Rivulet. Confirmed against
#: `Addons.GetAddons(type=xbmc.python.pluginsource, content=video)`
#: rather than guessed - re-check it if the capture machine's installed
#: add-ons change.
RIVULET_BROWSER_ROW = 2


def select_row(rpc, index):
    """Focus Home's row `index` and open it.

    Counts DOWN ONLY, from the freshly-built HomeWindow `goto_home()`
    guarantees, whose selection starts at row 0. It must not try to
    "return to the top" first: Kodi's lists WRAP, so Input.Up from row 0
    lands on the last row, and a fixed run of Ups walks the selection
    somewhere unpredictable - which is how one capture opened Kodi's addon
    settings dialog (window 10140) instead of the Movies picker.
    """
    for _ in range(index):
        rpc.call("Input.Down")
        time.sleep(0.3)
    rpc.call("Input.Select")


def main():
    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    rpc = wait_for_kodi()
    print("Kodi JSON-RPC ready.")

    goto_home(rpc, delay=3.0)
    curate(take_screenshot(rpc, "home"), "home", redact=True)

    select_row(rpc, HOME_MOVIES)
    time.sleep(3.0)
    curate(take_screenshot(rpc, "discover_catalogs"), "discover-catalogs")

    rpc.call("Input.Select")  # first catalog -> coverflow
    time.sleep(3.5)
    curate(take_screenshot(rpc, "discover_coverflow"), "discover-coverflow")

    rpc.call("Input.Select")  # a title -> detail + streams
    time.sleep(3.5)
    curate(take_screenshot(rpc, "detail_streams"), "detail-streams")

    rpc.call("Input.Select")  # a stream -> the "Preparing stream" dialog
    time.sleep(3.0)
    curate(take_screenshot(rpc, "resolving_stream"), "resolving-stream")
    rpc.call("Input.Back")  # cancel - real resolution can take minutes/fail
    time.sleep(1.5)

    goto_home(rpc)
    select_row(rpc, HOME_SEARCH)
    time.sleep(2.5)
    curate(take_screenshot(rpc, "search"), "search")

    rpc.call("Input.Select")  # open keyboard
    time.sleep(1.5)
    rpc.call("Input.SendText", {"text": "star wars", "done": True})
    # The fan-out queries every installed addon; 8s was not enough and the
    # shot caught the spinner instead of results.
    time.sleep(16.0)
    curate(take_screenshot(rpc, "search_results"), "search-results")

    goto_home(rpc)
    select_row(rpc, HOME_LIBRARY)
    time.sleep(2.5)
    curate(take_screenshot(rpc, "library"), "library")

    goto_home(rpc)
    select_row(rpc, HOME_ADDONS)
    time.sleep(2.5)
    curate(take_screenshot(rpc, "addons_manager"), "addons-manager")

    # Kodi's own add-on browser. Jump straight to the Video add-ons folder
    # by path rather than counting rows down the category list - that count
    # is a property of the user's Kodi install, and the old fixed 11 landed
    # in "Audio encoders" here.
    rpc.call("Input.Back")   # close AddonsWindow -> Rivulet Home
    time.sleep(1.0)
    rpc.call("Input.Back")   # close Rivulet -> Kodi shell
    time.sleep(1.5)
    rpc.call("GUI.ActivateWindow",
             {"window": "addonbrowser", "parameters": ["addons://user/xbmc.addon.video/"]})
    time.sleep(2.0)
    # Sorted by name with ".." first, so Rivulet sits at RIVULET_BROWSER_ROW.
    # Verified against Addons.GetAddons rather than assumed.
    for _ in range(RIVULET_BROWSER_ROW):
        rpc.call("Input.Down")
        time.sleep(0.25)
    curate(take_screenshot(rpc, "kodi_video_addons"), "kodi-addon-browser")

    rpc.call("Input.Select")  # addon info dialog
    time.sleep(1.2)
    curate(take_screenshot(rpc, "kodi_addon_info"), "kodi-addon-info")

    print(f"Done. Curated screenshots in {OUT_DIR}")


if __name__ == "__main__":
    main()
