# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""The phone pairing page. No Kodi. The live check binds 127.0.0.1 only."""
import contextlib
import io
import socket
import struct
import urllib.error
import urllib.request

from lib.pairing import (
    MAX_BODY,
    MAX_FAILURES,
    MAX_KEY_LENGTH,
    MAX_TEXT_LENGTH,
    MAX_URL_LENGTH,
    TTL_SECONDS,
    PairServer,
    PairSession,
    body_limit,
    clean_key,
    clean_text,
    clean_url,
    local_ipv4,
    new_code,
    respond,
    write_qr_bmp,
)
from lib.qrcodegen import QrCode

_SECRET = 'SUPERSECRETKEY'
_STRINGS = {
    'title': 'OMDb',
    'prompt': 'Paste the key',
    'save': 'Save',
    'saved': 'Saved. You can close this page.',
    'bad': 'That code does not match.',
    'empty': 'Enter a key.',
    'used': 'Already saved.',
    'expired': 'That code expired.',
    'long': 'That key is too long.',
}

# Captured before the suite's socket guard replaces them.
_REAL_CONNECT = socket.socket.connect
_REAL_CREATE = socket.create_connection


def _allow_localhost(monkeypatch):
    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else ''
        if host != '127.0.0.1':
            raise AssertionError('real network access attempted in a unit test')
        return _REAL_CONNECT(self, address)

    def create_connection(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else ''
        if host != '127.0.0.1':
            raise AssertionError('real network access attempted in a unit test')
        return _REAL_CREATE(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket, 'create_connection', create_connection)


def _request(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method='POST' if data is not None else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, resp.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8')


def test_code_is_short_and_unambiguous():
    # The phone-page string says the code works for 3 minutes.
    assert TTL_SECONDS == 180
    code = new_code()
    assert len(code) == 6
    assert set(code) <= set('ABCDEFGHJKLMNPQRSTUVWXYZ23456789')
    assert '0' not in code and 'O' not in code and '1' not in code and 'I' not in code


def test_submit_stores_one_key_and_then_refuses():
    session = PairSession(code='ABCDEF', now=lambda: 0, ttl=TTL_SECONDS)
    assert session.submit('ABCDEF', '  %s  ' % _SECRET) == 'ok'
    assert session.submit('ABCDEF', 'OTHERSECRET') == 'used'
    assert session.take() == _SECRET
    assert session.take() is None


def test_a_wrong_code_does_not_store_the_key_and_then_locks():
    session = PairSession(code='ABCDEF', now=lambda: 0)
    for _ in range(MAX_FAILURES - 1):
        assert session.submit('ZZZZZZ', _SECRET) == 'bad'
    assert session.submit('ZZZZZZ', _SECRET) == 'expired'
    assert session.submit('ABCDEF', _SECRET) == 'expired'
    assert session.take() is None


def test_an_expired_code_stores_nothing():
    clock = {'t': 0}
    session = PairSession(code='ABCDEF', now=lambda: clock['t'], ttl=180)
    clock['t'] = 181
    assert session.submit('ABCDEF', _SECRET) == 'expired'
    assert session.take() is None


def test_blank_and_overlong_keys_are_refused():
    session = PairSession(code='ABCDEF', now=lambda: 0)
    assert clean_key('   ') == ''
    assert session.submit('ABCDEF', ' \n ') == 'empty'
    assert session.submit('ABCDEF', 'A' * (MAX_KEY_LENGTH + 1)) == 'long'
    assert session.take() is None
    assert session.submit('ABCDEF', 'A' * MAX_KEY_LENGTH) == 'ok'


def test_the_page_escapes_its_title_and_never_prints_the_key():
    session = PairSession(code='ABCDEF', now=lambda: 0)
    strings = dict(_STRINGS, title='<b>OMDb</b>')
    _status, html = respond('GET', '/ABCDEF', b'', session, strings)
    assert '<b>' not in html
    assert '&lt;b&gt;' in html
    body = ('code=WRONG&key=%s' % _SECRET).encode('utf-8')
    status, html = respond('POST', '/ABCDEF', body, session, strings)
    assert status == 403
    assert _SECRET not in html
    body = ('code=ABCDEF&key=%s' % _SECRET).encode('utf-8')
    status, html = respond('POST', '/ABCDEF', body, session, strings)
    assert status == 200
    assert _SECRET not in html
    assert 'Saved' in html
    assert session.take() == _SECRET


def test_local_ipv4_is_empty_without_a_route(monkeypatch):
    def boom(self, address):
        raise OSError('no route')

    monkeypatch.setattr(socket.socket, 'connect', boom)
    assert local_ipv4() == ''


def test_the_server_accepts_one_post_and_does_not_log_the_key(monkeypatch):
    _allow_localhost(monkeypatch)
    session = PairSession(code='ABCDEF', now=lambda: 0)
    server = PairServer(session, _STRINGS, host='127.0.0.1')
    stderr = io.StringIO()
    try:
        server.start()
        url = server.page_url('127.0.0.1')
        with contextlib.redirect_stderr(stderr):
            status, page = _request(url)
            assert status == 200
            assert 'ABCDEF' in page
            posted, saved = _request(url, data=('key=%s' % _SECRET).encode('utf-8'))
            huge = ('key=%s&pad=%s' % (_SECRET, 'x' * 3000)).encode('utf-8')
            rejected, reject_page = _request(url, data=huge, headers={'Content-Length': str(len(huge))})
        assert posted == 200
        assert _SECRET not in saved
        assert _SECRET not in page
        assert rejected == 400
        assert _SECRET not in reject_page
        assert session.take() == _SECRET
    finally:
        server.close()
    assert _SECRET not in stderr.getvalue()


def test_qr_bmp_paints_the_finder_and_leaves_the_quiet_zone(tmp_path):
    text = 'http://127.0.0.1:9/ABCDEF'
    path = tmp_path / 'code.bmp'
    write_qr_bmp(text, str(path))
    data = path.read_bytes()
    assert data[:2] == b'BM'
    offset = struct.unpack_from('<I', data, 10)[0]
    width, height = struct.unpack_from('<ii', data, 18)
    stride = (width * 3 + 3) & ~3

    def pixel(x, y):
        start = offset + (height - 1 - y) * stride + x * 3
        return data[start:start + 3]

    assert pixel(2, 2) == b'\xff\xff\xff'
    assert pixel(4 * 8 + 4, 4 * 8 + 4) == b'\x00\x00\x00'
    qr = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    assert qr.get_module(0, 0) is True


def test_clean_url_keeps_a_link_and_reads_stremio_links_as_https():
    assert clean_url(' https://a.example/manifest.json ') == 'https://a.example/manifest.json'
    assert clean_url('stremio://a.example/x/manifest.json') == 'https://a.example/x/manifest.json'
    assert clean_url('STREMIO://a.example/m.json') == 'https://a.example/m.json'
    assert clean_url('http://192.168.1.2:7000/manifest.json') == 'http://192.168.1.2:7000/manifest.json'


def test_clean_url_refuses_other_schemes_spaces_and_overlong_links():
    assert clean_url('') == ''
    assert clean_url(None) == ''
    assert clean_url('javascript:alert(1)') == ''
    assert clean_url('ftp://a.example/m.json') == ''
    assert clean_url('https://a.example/ m.json') == ''
    assert clean_url('https://a.example/' + 'x' * MAX_URL_LENGTH) == ''


def test_clean_text_collapses_whitespace_and_drops_control_characters():
    assert clean_text('  the \t dark\nknight ') == 'the dark knight'
    assert clean_text('bat\x00man') == 'batman'
    assert clean_text('   ') == ''
    assert clean_text('x' * (MAX_TEXT_LENGTH + 1)) == ''
    assert clean_text('x' * MAX_TEXT_LENGTH) == 'x' * MAX_TEXT_LENGTH


def test_a_link_session_takes_a_long_link_and_measures_refusals_against_its_cap():
    session = PairSession(code='ABCDEF', now=lambda: 0, cleaner=clean_url, max_length=MAX_URL_LENGTH)
    configured = 'https://a.example/' + 'c' * 1500 + '/manifest.json'
    assert session.submit('ABCDEF', 'not a link') == 'empty'
    assert session.submit('ABCDEF', 'https://a.example/' + 'x' * MAX_URL_LENGTH) == 'long'
    assert session.submit('ABCDEF', configured) == 'ok'
    assert session.take() == configured


def test_a_search_session_takes_text():
    session = PairSession(code='ABCDEF', now=lambda: 0, cleaner=clean_text, max_length=MAX_TEXT_LENGTH)
    assert session.submit('ABCDEF', '   ') == 'empty'
    assert session.submit('ABCDEF', 'x' * (MAX_TEXT_LENGTH + 5)) == 'long'
    assert session.submit('ABCDEF', ' blade  runner ') == 'ok'
    assert session.take() == 'blade runner'


def test_only_a_link_page_accepts_a_bigger_post():
    assert body_limit(PairSession(code='ABCDEF')) == MAX_BODY
    link_session = PairSession(code='ABCDEF', cleaner=clean_url, max_length=MAX_URL_LENGTH)
    # A percent-encoded link can be three times its length.
    assert body_limit(link_session) >= MAX_URL_LENGTH * 3


def test_the_page_links_only_an_http_configure_page_and_sizes_its_field():
    session = PairSession(code='ABCDEF', now=lambda: 0, cleaner=clean_url, max_length=MAX_URL_LENGTH)
    strings = dict(_STRINGS, link='https://cfg.example/configure?a=1&b="x"', link_label='Configure <it>')
    status, html = respond('GET', '/ABCDEF', b'', session, strings)
    assert status == 200
    assert 'href="https://cfg.example/configure?a=1&amp;b=&quot;x&quot;"' in html
    assert 'rel="noopener noreferrer"' in html
    assert 'Configure &lt;it&gt;' in html
    assert 'maxlength="%d"' % MAX_URL_LENGTH in html
    strings['link'] = 'javascript:alert(1)'
    _status, html = respond('GET', '/ABCDEF', b'', session, strings)
    assert 'javascript' not in html
    assert '<a ' not in html


def test_a_key_page_has_no_outbound_link_and_keeps_its_field_size():
    session = PairSession(code='ABCDEF', now=lambda: 0)
    _status, html = respond('GET', '/ABCDEF', b'', session, _STRINGS)
    assert '<a ' not in html
    assert 'maxlength="%d"' % MAX_KEY_LENGTH in html


def test_lowercase_code_and_path_are_accepted_and_close_revokes():
    session = PairSession(code='ABCDEF')
    status, _ = respond('POST', '/abcdef', b'key=value', session, _STRINGS)
    assert status == 200 and session.take() == 'value'
    assert session.submit('  abcdef  ', 'value') == 'ok'
    session.close()
    assert session.locked() and session.expired()
    assert session.submit('ABCDEF', 'late') == 'expired'
    assert session.take() is None


def test_partial_post_handler_ends_when_server_closes(monkeypatch):
    import threading

    from lib import pairing

    _allow_localhost(monkeypatch)
    entered, ended = threading.Event(), threading.Event()
    original = pairing._Handler.do_POST
    def post(handler):
        entered.set()
        try:
            original(handler)
        finally:
            ended.set()
    monkeypatch.setattr(pairing._Handler, 'do_POST', post)
    session = PairSession()
    server = PairServer(session, _STRINGS, host='127.0.0.1')
    server.start()
    try:
        with socket.create_connection(('127.0.0.1', server.port), timeout=5) as sock:
            sock.sendall(b'POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 50\r\n\r\nke')
            assert entered.wait(5)
            server.close()
            assert ended.wait(5)  # Hang guard; no elapsed-time assertion.
            assert session.take() is None
    finally:
        server.close()


def test_keepalive_cannot_save_after_server_close(monkeypatch):
    _allow_localhost(monkeypatch)
    session = PairSession()
    server = PairServer(session, _STRINGS, host='127.0.0.1')
    server.start()
    try:
        with socket.create_connection(('127.0.0.1', server.port), timeout=5) as sock:
            sock.sendall(b'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n')
            data = b''
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            assert b'Connection: close' in data
            server.close()
            try:
                sock.sendall(b'POST / HTTP/1.1\r\nContent-Length: 9\r\n\r\nkey=value')
                late = sock.recv(4096)
            except OSError:
                late = b''
            assert b'Saved' not in late and session.take() is None
    finally:
        server.close()


def test_fifth_connection_is_closed_without_a_response(monkeypatch):
    from lib import pairing

    accepted, rejected = [], []
    monkeypatch.setattr(pairing.ThreadingHTTPServer, 'process_request', lambda self, request, address: accepted.append(request))
    server = pairing._Server(('127.0.0.1', 0), PairSession(), _STRINGS)
    monkeypatch.setattr(server, 'shutdown_request', rejected.append)
    requests = [object() for _ in range(5)]
    try:
        for request in requests:
            server.process_request(request, ('127.0.0.1', 1))
        assert accepted == requests[:4] and rejected == requests[4:]
        for request in accepted:
            server._release_connection(request)
    finally:
        server.server_close()
