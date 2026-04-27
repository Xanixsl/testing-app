"""HTTP-клиент с прокачанными заголовками и мягкими таймаутами."""
from __future__ import annotations

import socket

import requests
from urllib3.util import connection as _u3_conn

# ВНИМАНИЕ: НЕ ставим глобально allowed_gai_family = AF_INET — это ломает
# api.telegram.org (он на нашем shared-хосте доступен только по IPv6).
# Вместо этого экспортируем helper, который форсит IPv4 для конкретного
# Session/запроса. См. `make_ipv4_session()` ниже.

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 VeloraSound/1.0"
)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
        }
    )
    return s


# --- IPv4-only helpers (для googlevideo / SoundCloud CDN, у которых v6 отвалена)
_orig_create_connection = _u3_conn.create_connection


def _create_connection_ipv4(address, *args, **kwargs):
    """`urllib3.util.connection.create_connection`, режущий gai до AF_INET."""
    host, port = address
    err = None
    for res in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
        af, socktype, proto, _canonname, sa = res
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            timeout = kwargs.get("timeout", _u3_conn._DEFAULT_TIMEOUT)
            if timeout is not _u3_conn._DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            src = kwargs.get("source_address")
            if src:
                sock.bind(src)
            sock.connect(sa)
            return sock
        except OSError as e:
            err = e
            if sock is not None:
                sock.close()
    if err is not None:
        raise err
    raise OSError("getaddrinfo returned no AF_INET addresses for %r" % (host,))


class _IPv4HTTPAdapter(requests.adapters.HTTPAdapter):
    """HTTPAdapter, который для своих коннектов форсит IPv4 через monkey-patch
    `urllib3.util.connection.create_connection` на время `send()`."""

    def send(self, *args, **kwargs):  # type: ignore[override]
        prev = _u3_conn.create_connection
        _u3_conn.create_connection = _create_connection_ipv4
        try:
            return super().send(*args, **kwargs)
        finally:
            _u3_conn.create_connection = prev


def make_ipv4_session() -> requests.Session:
    """Сессия `requests`, ходящая ТОЛЬКО по IPv4 (для CDN с битым v6)."""
    s = make_session()
    adapter = _IPv4HTTPAdapter()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# Глобальный SESSION форсит IPv4: на shared-хостинге Sprinthost у CDN
# googlevideo / cf-media.sndcdn.com / lrclib.net IPv6 либо отсутствует, либо
# отвечает таймаутом — обычные `requests` зависают на 20+ сек, и мы получаем
# «тексты не грузятся / стрим обрывается». IPv4-only — самое надёжное.
# api.telegram.org (только v6 на этом хосте) ходит через свой собственный
# session внутри velora/auth.py, его IPv4-форс не затрагивает.
SESSION = make_ipv4_session()
DEFAULT_TIMEOUT = 6
