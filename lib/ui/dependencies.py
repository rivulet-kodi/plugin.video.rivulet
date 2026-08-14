"""Process-wide UI dependency provider.

Exactly one `Store` and one `AddonClient` back the whole plugin process:
`views.py` and `player.py` both read/write the same on-disk state and hit
the same addon HTTP endpoints, so they MUST share instances rather than
each keeping its own lazily-constructed singleton.

`Store`/`AddonClient` are resolved lazily, on first get_store()/
get_client() call, rather than imported at module scope:
`lib.stremio.addons` lazily imports `requests` (~200 transitive modules)
itself on first `AddonClient()` construction, and nearly every UI module
imports this one just for get_store() - a directory listing with zero
HTTP calls (e.g. the home screen) must not pay any of that cost merely by
importing `dependencies`. `Store`/`AddonClient` stay module-level names
(rather than function-local imports) so tests can still monkeypatch
`dependencies.Store`/`dependencies.AddonClient` directly, exactly as
before this module became lazy; `None` marks "not resolved yet" (a real
Store/AddonClient class is never `None`), so a monkeypatched fake is
honoured without triggering a redundant real import.
"""
from lib.ui.compat import addon_profile_dir

_STORE = None
_CLIENT = None

Store = None
AddonClient = None


def _resolve_store():
    global Store
    if Store is None:
        from lib.store import Store as _Store
        Store = _Store
    return Store


def _resolve_addon_client():
    global AddonClient
    if AddonClient is None:
        from lib.stremio.addons import AddonClient as _AddonClient
        AddonClient = _AddonClient
    return AddonClient


def get_store():
    global _STORE
    if _STORE is None:
        _STORE = _resolve_store()(addon_profile_dir())
    return _STORE


def get_client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _resolve_addon_client()()
    return _CLIENT
