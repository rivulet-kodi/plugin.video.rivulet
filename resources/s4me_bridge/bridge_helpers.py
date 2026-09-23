# SPDX-License-Identifier: GPL-3.0-only
#
# Copyright (C) 2026 the Rivulet contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""Pure, Kodi-independent AND Stream4Me-independent logic for the S4Me
bridge (`bridge.py`, in this same directory).

Licensed GPL-3, unlike the rest of this MIT-licensed addon: this whole
`resources/s4me_bridge/` directory only exists to glue into the GPL-3
Stream4Me (`plugin.video.s4me`) addon at runtime, and ships as an
opt-in-only feature (see `s4me_enable`, default off, in `resources/settings.xml`).

This module is deliberately its OWN copy of the request/response shaping
logic, rather than importing `lib.s4me` (Rivulet's `lib` package) or
anything from Stream4Me itself:

  - `bridge.py` runs as a separate `RunScript()` invocation with
    Stream4Me's own root (and its vendored `lib/`) prepended to
    `sys.path` so ITS internal `from core import ...`/`from lib import
    ...` absolute imports resolve. Rivulet's addon root also ships a
    top-level `lib` package of its own -- if `bridge.py` ever imported it
    (`from lib import s4me`) while Stream4Me's `lib/` sits earlier on
    `sys.path`, `import lib` would silently resolve to WHICHEVER of the
    two happened to be found first, depending on path order and import
    cache state. The only reliable fix is to never let this process
    import a module literally named `lib` that belongs to Rivulet at all.
  - Being self-contained also keeps this module importable and testable
    (see `tests/test_s4me_bridge_helpers.py`) with neither Kodi nor
    Stream4Me installed -- exactly the guarantee `lib/s4me.py` itself
    keeps for Rivulet's own side of this feature.

`MANIFEST` here is kept in sync BY HAND with `lib/s4me.MANIFEST` -- see
that module's docstring. `tests/test_s4me.py` and
`tests/test_s4me_bridge_helpers.py` both assert the two are identical,
which pins them together across any future edit to either.
"""
import re
import time
import unicodedata
from urllib.parse import parse_qsl

#: Stremio addon manifest this bridge serves at `/manifest.json` -- see
#: this module's docstring for why it is a second, hand-synced copy of
#: `lib.s4me.MANIFEST` rather than an import of it.
MANIFEST = {
    "id": "org.rivulet.s4me",
    "name": "Stream4Me",
    "version": "1.0.0",
    "description": (
        "Bridges the Stream4Me (S4Me) Kodi addon's Italian channel "
        "scrapers into the Stremio protocol."
    ),
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
}

#: A Stremio stream request id is either a bare IMDb id ("tt1234567", for
#: a movie) or an IMDb id plus 1-based season/episode ("tt1234567:1:2",
#: for a series episode) -- see https://github.com/Stremio/stremio-addon-sdk
#: "Request stream" for the wire format this matches.
_STREAM_ID_RE = re.compile(r"^(tt\d+)(?::(\d+):(\d+))?$")


def parse_stream_id(id_):
    """Split a Stremio `/stream/{type}/{id}.json` id into `(imdb_id,
    season, episode)` -- `season`/`episode` are `None` for a movie id.
    Returns `None` for anything that doesn't match the expected shape
    (never raises)."""
    match = _STREAM_ID_RE.match((id_ or "").strip())
    if not match:
        return None
    imdb_id, season, episode = match.groups()
    return imdb_id, (int(season) if season else None), (int(episode) if episode else None)


def split_kodi_url(raw):
    """Split a Stream4Me server-item url's Kodi-style
    `"url|Header=value&Header2=value2"` shape into `(url, headers)`.

    No `|` present -> `(raw, {})`. Header values are `%`-decoded (Stream4Me
    encodes them with `urllib.parse.urlencode`, the standard Kodi
    resolved-url convention for per-request headers); a malformed/empty
    header segment yields an empty headers dict, never raises. `raw is
    None` -> `("", {})`.
    """
    if raw is None:
        return "", {}
    url, sep, query = raw.partition("|")
    if not sep or not query:
        return url, {}
    headers = dict(parse_qsl(query, keep_blank_values=True))
    return url, headers


def normalize_title(text):
    """Lowercase, accent-stripped, punctuation-collapsed form of `text`,
    for fallback title+year matching when a search result's infoLabels
    carries no `tmdb_id`."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", stripped.lower()).strip()


def tmdb_id_matches(candidate_tmdb_id, target_tmdb_id):
    """True if both identify the same, present TMDB id. A missing/empty
    `target_tmdb_id` never matches anything (there is nothing to confirm
    against), so callers fall back to `title_matches()` in that case."""
    if not target_tmdb_id:
        return False
    candidate = str(candidate_tmdb_id or "")
    return candidate != "" and candidate == str(target_tmdb_id)


def title_matches(candidate_title, candidate_year, target_title, target_year):
    """Fallback match when no `tmdb_id` is available on either side:
    normalized titles must be equal, and if BOTH sides carry a year, it
    must match too (a missing year on either side is not a mismatch --
    plenty of scraped titles carry no year at all)."""
    candidate_norm = normalize_title(candidate_title)
    if not candidate_norm or candidate_norm != normalize_title(target_title):
        return False
    if candidate_year and target_year:
        return str(candidate_year) == str(target_year)
    return True


def result_matches(info_labels, target_tmdb_id, target_title, target_year):
    """True if `info_labels` (a Stream4Me `Item.infoLabels`-like mapping)
    identifies the same title as `target_tmdb_id`/`target_title`/
    `target_year` -- `tmdb_id` first, normalized title+year fallback."""
    info_labels = info_labels or {}
    if tmdb_id_matches(info_labels.get("tmdb_id"), target_tmdb_id):
        return True
    return title_matches(
        info_labels.get("title") or info_labels.get("originaltitle"),
        info_labels.get("year"),
        target_title,
        target_year,
    )


def shape_stream(channel, server_name, url, headers=None, quality=None):
    """Build one Stremio `stream` object (an entry of the
    `/stream/{type}/{id}.json` response's `streams[]` array) from one
    resolved Stream4Me play url.

    `name` is always `"S4Me <channel>"` (Stremio groups streams by
    `name` in its UI, so every stream from one channel visually groups
    together); `title` carries the more specific per-result detail
    (server/quality) users actually pick between.
    """
    title_bits = [bit for bit in (server_name, quality) if bit]
    stream = {
        "name": "S4Me %s" % channel,
        "title": " - ".join(title_bits) if title_bits else channel,
        "url": url,
    }
    if headers:
        # notWebReady + proxyHeaders.request is the Stremio convention for
        # a direct-play url that needs custom request headers (referer/
        # cookie/user-agent) - see stremio-core's StreamBehaviorHints.
        stream["behaviorHints"] = {
            "notWebReady": True,
            "proxyHeaders": {"request": dict(headers)},
        }
    return stream


def build_manifest():
    """Return a fresh copy of `MANIFEST` -- callers may safely mutate
    their own copy without risk of corrupting the module-level constant
    (e.g. serializing it straight into an HTTP response body)."""
    return dict(MANIFEST)


def parse_channels_setting(raw):
    """Parse the `s4me_channels` comma-list setting into a tuple of
    stripped, non-empty channel ids, or `None` when blank -- `None` means
    "use every one of Stream4Me's own active channels" rather than a
    fixed allow-list. Identical logic to `lib.s4me.parse_channels_setting`
    (duplicated here on purpose -- see this module's docstring)."""
    if not raw or not raw.strip():
        return None
    return tuple(c.strip() for c in raw.split(",") if c.strip())


def select_channels(configured, active_channels):
    """Resolve the actual channel ids to search for one request.

    `configured` is `parse_channels_setting()`'s result: a tuple of
    explicit channel ids, or `None`. `active_channels` is the list of
    channel ids Stream4Me itself currently reports active (read from its
    `channels/<id>.json` `active` flags -- something only `bridge.py`
    itself knows how to determine, not this pure module).

    A `configured` id absent from `active_channels` is silently dropped
    (a stale/misspelled setting is never an error, just an empty
    contribution to the fan-out); `configured is None` returns every
    active channel unfiltered.
    """
    if configured is None:
        return tuple(active_channels)
    active_set = set(active_channels)
    return tuple(c for c in configured if c in active_set)


class TTLCache:
    """Tiny per-key time-to-live cache. `clock` is injectable
    (`time.monotonic` in production) so tests can control expiry
    deterministically without sleeping."""

    def __init__(self, ttl_seconds, clock=time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._store = {}

    def get(self, key):
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key, value):
        self._store[key] = (self._clock() + self._ttl, value)

    def __len__(self):
        return len(self._store)


class Budget:
    """A wall-clock deadline `bridge.py`'s per-request channel fan-out
    races against: "return whatever finished within ~N seconds" instead
    of blocking on every channel. `clock` is injectable for tests."""

    def __init__(self, seconds, clock=time.monotonic):
        self._deadline = clock() + seconds
        self._clock = clock

    def remaining(self):
        return max(0.0, self._deadline - self._clock())

    def expired(self):
        return self._clock() >= self._deadline
