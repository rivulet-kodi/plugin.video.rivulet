# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Which movie and series cards wear the watched check (issue #85).

A Home row is up to a few hundred `ListItem`s and the coverflow another
page, all built in bulk, so the verdict for a card has to be a set lookup:
no store read, and above all no per-card network call. `refresh()` computes
the set once per paint, and `is_watched()` is the lookup. This module only
holds that result between paints; every verdict is `lib.library`'s.

The set is built from what this process already has, never from a read of
its own:

* The account library as the service last pulled it
  (`Store.get_library_cache`, written by `lib.service_runner.ProgressPuller`
  every `LIBRARY_PULL_INTERVAL_SECONDS`), with this process's pending
  writes laid over it (`libraryaction.apply_library_writes`), so a mark
  made a moment ago shows before the next pull echoes it. That is what
  lets a Home with no Continue/Library row, and a coverflow opened in a
  fresh interpreter, show the account's answer with no `datastore_get`.
  With no cache yet (a fresh install, or a sign-in, before the first pull
  lands a few seconds later) the account's checks simply wait for that
  pull; local progress still marks what this device played.
* The last live read this process made, when Home's Continue/Library rows
  read the account anyway (`refresh(store, entries)`). Per title the newer
  record wins (`library.newer_verdicts`), so neither copy can roll the
  other back.
* Local `progress.json`, which holds this device's own plays the moment
  they happen, before the playback push reaches the account. Hence a title
  watched in Rivulet is marked right away.

How those combine, including the explicit "Mark as unwatched" case, is
`lib.library.watched_title_ids`. A series wears the check as soon as one
episode counts as watched: that is Stremio's own rule for every type
(`LibraryItem::watched()` is `times_watched > 0`, and a series' counter
goes up per episode), so the poster agrees with the official apps rather
than meaning "show finished".
"""
import threading

from lib import library

_LOCK = threading.Lock()
#: `lib.library.title_verdicts` of this process's last live library read.
#: Kept (rather than the raw records, which run to a kilobyte each) so a
#: later paint that did not read, such as the coverflow, still has it.
_live = {}
_marks: frozenset = frozenset()


def refresh(store, library_entries=None):
    """Recompute the set. Never raises: a card without its check is a
    cosmetic loss, a Home that fails to paint is not.

    `library_entries` is a live account read this paint already made
    (pending writes applied), or None. Signed out, every account answer is
    dropped: it belongs to the previous account.
    """
    global _live, _marks
    try:
        signed_in = bool(store.get_auth())
    except Exception:
        signed_in = False
    try:
        progress = store.get_progress_entries()
    except Exception:
        progress = []
    cached = {}
    if signed_in:
        from lib.ui.libraryaction import apply_library_writes

        try:
            records = store.get_library_cache()
        except Exception:
            records = []
        cached = library.title_verdicts(apply_library_writes(records))
    with _LOCK:
        if not signed_in:
            _live = {}
        elif library_entries is not None:
            _live = library.title_verdicts(library_entries)
        _marks = library.watched_title_ids(library.newer_verdicts(cached, _live), progress)


def is_watched(meta):
    """Whether `meta` (a catalog/library/progress record) should wear the
    check. A set lookup; see the module docstring. Ids are IMDb ids, unique
    across types, so the type is not part of the key."""
    return isinstance(meta, dict) and meta.get('id') in _marks


def reset():
    """Test hook. Production never calls this."""
    global _live, _marks
    with _LOCK:
        _live = {}
        _marks = frozenset()
