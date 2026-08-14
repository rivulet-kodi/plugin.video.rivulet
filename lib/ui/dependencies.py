"""Process-wide UI dependency provider.

Exactly one `Store` and one `AddonClient` back the whole plugin process:
`views.py` and `player.py` both read/write the same on-disk state and hit
the same addon HTTP endpoints, so they MUST share instances rather than
each keeping its own lazily-constructed singleton.
"""
from lib.store import Store
from lib.stremio.addons import AddonClient
from lib.ui.compat import addon_profile_dir

_STORE = None
_CLIENT = None


def get_store():
    global _STORE
    if _STORE is None:
        _STORE = Store(addon_profile_dir())
    return _STORE


def get_client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = AddonClient()
    return _CLIENT
