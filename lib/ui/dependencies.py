"""Process-wide UI dependency provider.

Exactly one `Store`, one `AddonClient`, and one `StremioAPI` back the
whole plugin process: `views.py`/`player.py` share the first two
already (see below); `lib.ui.detailwindow`'s library context menu adds
the third so its lookup-then-write round trip (a lookup via
`get_library_item()` immediately followed by a `put_library_item()` on
the SAME menu pick) reuses one `requests.Session()` rather than opening
a fresh one per call (`StremioAPI.__init__`'s own docstring already
notes it pools a Session per CLIENT instance, precisely so callers
share one).

`Store`/`AddonClient`/`StremioAPI` are resolved lazily, on first
get_store()/get_client()/get_api() call, rather than imported at module
scope: `lib.stremio.addons` lazily imports `requests` (~200 transitive
modules) itself on first `AddonClient()` construction, and nearly every
UI module imports this one just for get_store() - a directory listing
with zero HTTP calls (e.g. the home screen) must not pay any of that
cost merely by importing `dependencies`. `Store`/`AddonClient`/
`StremioAPI` stay module-level names (rather than function-local
imports) so tests can still monkeypatch `dependencies.Store`/
`dependencies.AddonClient`/`dependencies.StremioAPI` directly, exactly
as before this module became lazy; `None` marks "not resolved yet" (a
real Store/AddonClient/StremioAPI class is never `None`), so a
monkeypatched fake is honoured without triggering a redundant real
import.
"""
from lib.ui.compat import addon_profile_dir

_STORE = None
_CLIENT = None
_API = None

Store = None
AddonClient = None
StremioAPI = None


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


def _resolve_stremio_api():
    global StremioAPI
    if StremioAPI is None:
        from lib.stremio.api import StremioAPI as _StremioAPI
        StremioAPI = _StremioAPI
    return StremioAPI


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


def get_api():
    global _API
    if _API is None:
        _API = _resolve_stremio_api()()
    return _API
