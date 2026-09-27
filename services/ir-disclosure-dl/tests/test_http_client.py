from __future__ import annotations

import socket
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import http_client  # noqa: E402


class _FakeHTTPResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self._headers = headers or {}
        self.msg = _FakeMessage(self._headers)

    def read(self) -> bytes:
        return self._body

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name, default)


class _FakeMessage:
    """http.client.HTTPResponse.msgの構造的部分型(get_allのみ使用)。複数のSet-Cookie
    をテストする場合は、呼び出し側でheaders辞書のキーに連番を振らず、単一のCookie
    文字列を';'で連結する形に丸めている（簡易フェイクのため）。"""

    def __init__(self, headers: dict[str, str]) -> None:
        self._headers = headers

    def get_all(self, name: str, default: list[str]) -> list[str]:
        value = self._headers.get(name)
        return [value] if value is not None else default


class _FakeConnection:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self._index = 0
        self.requested: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def request(self, method: str, path: str, body: Any = None, headers: dict[str, str] = {}) -> None:
        self.requested.append((method, path, dict(headers)))

    def getresponse(self) -> _FakeHTTPResponse:
        item = self._responses[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        if len(item) == 3:
            status, body, headers = item
        else:
            status, body = item
            headers = {}
        return _FakeHTTPResponse(status, body, headers)

    def close(self) -> None:
        self.closed = True


def make_client_with_fake(*responses: Any) -> tuple[http_client.RetryingHttpClient, _FakeConnection]:
    client = http_client.RetryingHttpClient()
    fake = _FakeConnection(list(responses))
    client._conns[("example.com", 443, True)] = fake
    return client, fake


def test_get_reuses_cached_connection() -> None:
    client, fake = make_client_with_fake((200, b"first"), (200, b"second"))
    assert client.get("https://example.com/a") == b"first"
    assert client.get("https://example.com/b") == b"second"
    assert [(m, p) for m, p, _h in fake.requested] == [("GET", "/a"), ("GET", "/b")]


def test_get_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake = make_client_with_fake((429, b""), (200, b"ok"))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    assert client.get("https://example.com/x") == b"ok"


def test_get_fails_immediately_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake = make_client_with_fake((404, b""))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="404"):
        client.get("https://example.com/x")
    assert len(fake.requested) == 1


def test_get_fails_immediately_on_dns_resolution_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake = make_client_with_fake(socket.gaierror("Name or service not known"))
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleep_calls.append(seconds))
    with pytest.raises(RuntimeError, match="名前解決"):
        client.get("https://example.com/x")
    assert len(fake.requested) == 1
    assert sleep_calls == []


def test_get_follows_redirect_and_returns_final_body() -> None:
    client, fake = make_client_with_fake(
        (301, b"", {"Location": "/ir/index.html"}),
        (200, b"final content"),
    )
    assert client.get("https://example.com/ir/") == b"final content"
    assert [(m, p) for m, p, _h in fake.requested] == [("GET", "/ir/"), ("GET", "/ir/index.html")]


def test_post_form_sends_urlencoded_body() -> None:
    client, fake = make_client_with_fake((200, b"ok"))
    result = client.post_form("https://example.com/search", {"eqMgrCd": "1301", "dspSsuPd": "10"})
    assert result == b"ok"
    method, path, headers = fake.requested[0]
    assert method == "POST"
    assert path == "/search"
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"


def test_post_form_includes_referer_when_given() -> None:
    client, fake = make_client_with_fake((200, b"ok"))
    client.post_form("https://example.com/search", {"a": "1"}, referer="https://example.com/start")
    _method, _path, headers = fake.requested[0]
    assert headers["Referer"] == "https://example.com/start"


def test_cookie_is_stored_and_sent_on_next_request() -> None:
    client, fake = make_client_with_fake(
        (200, b"first", {"Set-Cookie": "JSESSIONID=abc123; Path=/tseHpFront"}),
        (200, b"second"),
    )
    client.get("https://example.com/a")
    client.get("https://example.com/b")

    _m0, _p0, headers0 = fake.requested[0]
    assert "Cookie" not in headers0

    _m1, _p1, headers1 = fake.requested[1]
    assert headers1["Cookie"] == "JSESSIONID=abc123"


def test_different_hosts_get_separate_connections() -> None:
    client = http_client.RetryingHttpClient()
    fake_a = _FakeConnection([(200, b"a")])
    fake_b = _FakeConnection([(200, b"b")])
    client._conns[("a.example.com", 443, True)] = fake_a
    client._conns[("b.example.com", 443, True)] = fake_b
    assert client.get("https://a.example.com/") == b"a"
    assert client.get("https://b.example.com/") == b"b"
