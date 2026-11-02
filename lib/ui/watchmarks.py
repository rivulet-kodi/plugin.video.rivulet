# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Which movie cards wear the watched check (issue #85).

A Home row is up to a few hundred `ListItem`s and the coverflow another
page, all built in bulk, so the verdict for a card has to be a set lookup:
no store read, and above all no per-card network call. `refresh()` computes
the set once per paint from the two sources that already exist in this
process, and `is_watched()` is the lookup.

* The account library (`newhome._fetch_library` feeds it in on every
  authenticated Home load, and the coverflow reads it itself when this
  process has not). `timesWatched > 0` is the verdict, as upstream.
* Local `progress.json`, which `lib.service_runner.ProgressPuller` fills
  from the account every few minutes (a flagged movie arrives as a 100%
  entry) and which holds this device's own plays the moment they happen,
  before the playback push reaches the account. Hence a movie watched in
  Rivulet is marked right away.

How the two combine, including the explicit "Mark as unwatched" case, is
`lib.library.watched_movie_ids`; this module only holds the result between
paints. Series get no mark: `timesWatched` counts episodes there, so the
account does not say whether the show as a whole is done.
"""
import threading

from lib import library

_LOCK = threading.Lock()
#: `lib.library.movie_verdicts` of the last library read. Kept (rather than
#: the raw records, which run to a kilobyte each) so a paint that has not
#: re-read the library, such as the coverflow, can still apply the account's
#: answer.
_verdicts = {}
_marks: frozenset = frozenset()
#: True once an account read has been applied for the signed-in account.
#: A fresh interpreter (the plugin route that opens a coverflow without
#: Home) starts False, and `account_loaded()` tells its caller to read.
_account_known = False


def refresh(store, library_entries=None):
    """Recompute the set. Never raises: a card without its check is a
    cosmetic loss, a Home that fails to paint is not.

    `library_entries` is a fresh account read, or None to reuse the last
    one. Signed out, the account's answer is dropped whatever was cached:
    it belongs to the previous account.
    """
    global _verdicts, _marks, _account_known
    try:
        signed_in = bool(store.get_auth())
    except Exception:
        signed_in = False
    try:
        progress = store.get_progress_entries()
    except Exception:
        progress = []
    with _LOCK:
        if not signed_in:
            _verdicts = {}
            _account_known = False
        elif library_entries is not None:
            _verdicts = library.movie_verdicts(library_entries)
            _account_known = True
        _marks = library.watched_movie_ids(_verdicts, progress)


def account_loaded():
    """Whether this process has applied an account library read since it
    last saw the account signed in."""
    return _account_known


def is_watched(meta):
    """Whether `meta` (a catalog/library/progress record) is a movie that
    should wear the check. A set lookup; see the module docstring."""
    if not isinstance(meta, dict) or meta.get('type') != 'movie':
        return False
    return meta.get('id') in _marks


def reset():
    """Test hook. Production never calls this."""
    global _verdicts, _marks, _account_known
    with _LOCK:
        _verdicts = {}
        _marks = frozenset()
        _account_known = False
