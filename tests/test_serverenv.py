# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Tests for lib.serverenv: the loopback-by-default listen address for the
embedded stremio-server-go (which, unset, listens unauthenticated on every
interface)."""
import pytest

from lib import serverenv


@pytest.mark.parametrize('url, expected', [
    ('http://127.0.0.1:11470', '127.0.0.1'),
    ('http://127.0.0.1', '127.0.0.1'),
    ('http://127.0.1.1:11470', '127.0.1.1'),  # all of 127/8 is loopback
    ('http://[::1]:11470', '::1'),
    ('http://localhost:11470', '127.0.0.1'),
    ('http://LOCALHOST:11470/', '127.0.0.1'),
    ('https://user:pw@127.0.0.1:12470', '127.0.0.1'),
])
def test_loopback_host_recognises_loopback_urls(url, expected):
    assert serverenv.loopback_host(url) == expected


@pytest.mark.parametrize('url', [
    'http://192.168.1.20:11470',  # LAN address: reachable on purpose
    'http://0.0.0.0:11470',  # wildcard, not loopback
    'http://stremio.lan:11470',  # a hostname is never assumed to be this machine
    'http://localhost.example.com:11470',
    'http://[::1',  # malformed IPv6 literal
    '',
    None,
    12345,
])
def test_loopback_host_is_none_for_everything_else(url):
    assert serverenv.loopback_host(url) is None


def test_overlay_pins_loopback_by_default():
    assert serverenv.bind_address_overlay('http://127.0.0.1:11470') == {'BIND_ADDRESS': '127.0.0.1'}
    assert serverenv.bind_address_overlay('http://[::1]:11470') == {'BIND_ADDRESS': '::1'}


@pytest.mark.parametrize('extra_env, base_env', [
    ({'BIND_ADDRESS': '0.0.0.0'}, None),  # chosen via the Kodi-settings overlay
    ({}, {'BIND_ADDRESS': '192.168.1.5'}),  # inherited from the process environment
    ({'STREMIO_ENABLE_DLNA': 'true'}, None),  # casting renderers fetch over the LAN
    ({'STREMIO_PROXY_PUBLIC_URL': 'https://proxy.example.com'}, None),
])
def test_overlay_steps_aside_when_lan_reachability_is_deliberate(extra_env, base_env):
    assert serverenv.bind_address_overlay('http://127.0.0.1:11470', extra_env, base_env) == {}


def test_overlay_still_pins_loopback_when_dlna_is_explicitly_off():
    extra = {'STREMIO_ENABLE_DLNA': 'false', 'STREMIO_PROXY_PUBLIC_URL': ''}
    assert serverenv.bind_address_overlay('http://127.0.0.1:11470', extra) == {'BIND_ADDRESS': '127.0.0.1'}


def test_overlay_is_empty_for_a_non_loopback_server_url():
    assert serverenv.bind_address_overlay('http://192.168.1.20:11470') == {}
