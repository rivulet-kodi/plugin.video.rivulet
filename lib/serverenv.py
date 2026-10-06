# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Environment policy shared by both ways of running the embedded server
(`lib.service_runner.ServerProcess` and `lib.libserver.LibraryServer`).

Pure (no `xbmc*`, no network): importable and testable with plain python3.

Why this module exists: stremio-server-go has no authentication - it is a
localhost-trust service - and when `BIND_ADDRESS` is unset it listens on
EVERY interface (`internal/app/app.go`: "no BIND_ADDRESS set; the
unauthenticated API is reachable from every network interface", for both the
HTTP and the HTTPS listener). The addon only pinned `APP_PATH`/`HTTP_PORT`, so
a default install - `server_url` = `http://127.0.0.1:11470` - exposed the
whole enginefs API (torrent engines, `/proxy`, `/settings`, ...) to the LAN of
every Kodi box it ran on, although the only client is Kodi itself, on the same
machine. `bind_address_overlay()` pins the listeners to loopback whenever the
configured `server_url` says that is where clients will connect, and steps
aside in every case where LAN reachability is deliberate.
"""

#: Env var stremio-server-go reads for the listen address (main.go/app.go).
BIND_ADDRESS_ENV = "BIND_ADDRESS"

#: `server_url` hostnames that mean "this machine" without being an IP
#: literal. Bound as the IPv4 loopback: a client that resolves `localhost` to
#: `::1` first simply falls back to 127.0.0.1 (every HTTP stack Kodi/requests
#: use tries each resolved address).
_LOOPBACK_NAMES = ("localhost",)


def loopback_host(server_url):
    """The loopback address `server_url` points at, or None.

    `127.0.0.0/8` and `::1` literals are returned as written (lowercased,
    without brackets); `localhost` maps to `127.0.0.1`. Anything else - a LAN
    IP, a hostname, an empty/garbled value - is None: the user pointed the
    addon at a non-loopback address on purpose (or at an external server), and
    this helper must never second-guess that.
    """
    import ipaddress
    from urllib.parse import urlparse

    try:
        host = urlparse(server_url).hostname
    except (ValueError, AttributeError):
        return None
    if not host:
        return None
    host = host.lower()
    if host in _LOOPBACK_NAMES:
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    return host if address.is_loopback else None


def bind_address_overlay(server_url, extra_env=None, base_env=None):
    """`{"BIND_ADDRESS": <loopback>}` to merge into the server's environment,
    or `{}` when the server must keep its historical all-interfaces default.

    The overlay is empty when:
    - `BIND_ADDRESS` is already chosen, in `extra_env` (the overlay Kodi
      settings produce) or in `base_env` (the inherited process environment):
      an explicit choice always wins;
    - `server_url` is not a loopback address: the user is addressing this
      server over the network, so it has to listen there;
    - `STREMIO_ENABLE_DLNA` is `true`: DLNA/casting renderers fetch media from
      the server over the LAN;
    - `STREMIO_PROXY_PUBLIC_URL` is set: a public proxy URL means other hosts
      are meant to reach `/proxy`.
    """
    extra_env = extra_env or {}
    if extra_env.get(BIND_ADDRESS_ENV) or (base_env or {}).get(BIND_ADDRESS_ENV):
        return {}
    if str(extra_env.get("STREMIO_ENABLE_DLNA", "")).lower() == "true":
        return {}
    if extra_env.get("STREMIO_PROXY_PUBLIC_URL"):
        return {}
    host = loopback_host(server_url)
    if host is None:
        return {}
    return {BIND_ADDRESS_ENV: host}
