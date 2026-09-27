"""任意ホストへのHTTP接続をKeep-Aliveで使い回すクライアント。
services/edinet-dl/scripts/fetch_documents.py の EdinetHttpClient と同じリトライ/
バックオフ設計（429・5xx・ネットワークエラーは指数バックオフで最大5回リトライ、
それ以外の4xxは即座に失敗）を、単一ホスト固定ではなく任意URL宛に一般化したもの。
TDnet日次一覧の取得（GET）と、東証上場会社情報サービスへのセッション付きフォーム
送信（GET+POST、Cookie維持）の両方で使う。

DNS解決失敗(socket.gaierror)は恒久的なエラーのため即座に失敗させる（リトライしても
絶対に解決しない）。リダイレクト(301/302/303/307/308)はLocationヘッダを追跡する
（ir-url-retriever開発時の実機検証で必要性を確認済み、詳細は
services/ir-disclosure-dl/docs/jpx_disclosure_design.md参照）。

Cookie管理: 東証上場会社情報サービス（www2.jpx.co.jp）はJSESSIONIDベースの
セッションを使い、検索→会社詳細の2段階POSTを同一セッションで行う必要がある
（詳細はdocs/jpx_disclosure_design.md参照）。Set-Cookieを受け取ったホストに対し、
以後の全リクエストへ自動的にCookieヘッダを付与する簡易Cookie jarを内蔵する
（`http.cookiejar`は使わずシンプルな辞書で十分、ドメイン・パス単位の厳密な
スコープ管理は行わない＝単一ホストへの逐次アクセスのみを想定した割り切り）。
"""
from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.parse
from typing import Any, Protocol, cast

USER_AGENT = "Mozilla/5.0 (compatible; ir-disclosure-dl/1.0)"


class _HTTPResponseLike(Protocol):
    status: int

    def read(self) -> bytes: ...


class _HTTPConnectionLike(Protocol):
    def request(self, method: str, url: str, body: Any = None, headers: dict[str, str] = ...) -> None: ...
    def getresponse(self) -> _HTTPResponseLike: ...
    def close(self) -> None: ...


class RetryingHttpClient:
    """ホストごとにhttp.client.HTTPSConnectionをキャッシュ・使い回す。任意URL宛。
    ホストごとに簡易Cookie jarを持つ（同一ホストへの一連のリクエストで自動送信）。
    """

    def __init__(self, default_timeout: float = 15) -> None:
        self._default_timeout = default_timeout
        self._conns: dict[tuple[str, int, bool], _HTTPConnectionLike] = {}
        self._cookies: dict[str, dict[str, str]] = {}

    def close(self) -> None:
        for conn in self._conns.values():
            conn.close()
        self._conns.clear()

    def _get_conn(self, host: str, port: int, use_https: bool, timeout: float) -> _HTTPConnectionLike:
        """ホスト単位で接続をキャッシュする。宛先ホストは実際には常に単一の用途に
        固定されるため、キャッシュ済み接続のタイムアウトを都度更新する必要はない
        （初回接続時のtimeoutがそのホストの実効値になる）。"""
        key = (host, port, use_https)
        conn = self._conns.get(key)
        if conn is None:
            cls = http.client.HTTPSConnection if use_https else http.client.HTTPConnection
            conn = cls(host, port, timeout=timeout)
            self._conns[key] = conn
        return conn

    def _cookie_header(self, host: str) -> str | None:
        jar = self._cookies.get(host)
        if not jar:
            return None
        return "; ".join(f"{k}={v}" for k, v in jar.items())

    def _store_cookies(self, host: str, set_cookie_values: list[str]) -> None:
        if not set_cookie_values:
            return
        jar = self._cookies.setdefault(host, {})
        for raw in set_cookie_values:
            # "NAME=VALUE; Path=...; HttpOnly" のような形式から先頭のNAME=VALUEだけを使う
            first_pair = raw.split(";", 1)[0].strip()
            if "=" in first_pair:
                name, value = first_pair.split("=", 1)
                jar[name] = value

    def _request_once(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
        max_retries: int,
        timeout: float | None,
    ) -> tuple[int, str | None, bytes]:
        """リダイレクトは追わず、1つのURLに対する応答(status, Locationヘッダ, body)を返す。
        呼び出し前後でこのホスト宛のCookieを自動的に送受信する。"""
        parsed = urllib.parse.urlsplit(url)
        use_https = parsed.scheme == "https"
        port = parsed.port or (443 if use_https else 80)
        host = parsed.hostname
        if host is None:
            raise ValueError(f"不正なURL(ホスト不明): {url}")
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        key = (host, port, use_https)
        effective_timeout = timeout if timeout is not None else self._default_timeout

        request_headers = {"User-Agent": USER_AGENT, **headers}
        cookie_header = self._cookie_header(host)
        if cookie_header:
            request_headers["Cookie"] = cookie_header

        attempt = 0
        while True:
            attempt += 1
            try:
                conn = self._get_conn(host, port, use_https, effective_timeout)
                conn.request(method, path, body=body, headers=request_headers)
                resp = conn.getresponse()
                data = resp.read()
            except socket.gaierror as e:
                # 名前解決失敗は恒久的なエラーで、リトライしても絶対に解決しない。
                self._conns.pop(key, None)
                raise RuntimeError(f"{url}: 名前解決に失敗しました ({e})") from e
            except (http.client.HTTPException, OSError) as e:
                self._conns.pop(key, None)
                if attempt > max_retries:
                    raise RuntimeError(f"{url}: ネットワークエラーが続くためリトライ上限に達しました ({e})") from e
                time.sleep(min(60, 2**attempt))
                continue

            if resp.status == 429 or 500 <= resp.status < 600:
                if attempt > max_retries:
                    raise RuntimeError(f"{url}: リトライ上限に達しました (status={resp.status})")
                time.sleep(min(60, 2**attempt))
                continue

            if resp.status >= 400:
                raise RuntimeError(f"{url}: HTTPエラー status={resp.status}")

            msg = cast(Any, resp).msg
            set_cookie_values = list(msg.get_all("Set-Cookie", [])) if msg is not None else []
            self._store_cookies(host, set_cookie_values)

            location = cast(Any, resp).getheader("Location")
            return resp.status, location, data

    _REDIRECT_STATUSES = {301, 302, 303, 307, 308}

    def get(self, url: str, max_retries: int = 5, timeout: float | None = None, max_redirects: int = 5) -> bytes:
        current_url = url
        for _ in range(max_redirects + 1):
            status, location, data = self._request_once("GET", current_url, None, {}, max_retries, timeout)
            if status in self._REDIRECT_STATUSES and location:
                current_url = urllib.parse.urljoin(current_url, location)
                continue
            return data
        raise RuntimeError(f"{url}: リダイレクトが{max_redirects}回を超えました")

    def post_form(
        self,
        url: str,
        fields: dict[str, str],
        referer: str | None = None,
        max_retries: int = 5,
        timeout: float | None = None,
    ) -> bytes:
        """application/x-www-form-urlencoded でPOSTする。東証上場会社情報サービスの
        フォーム送信（実機解析結果はdocs/jpx_disclosure_design.md参照）で使う。"""
        body = urllib.parse.urlencode(fields).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))}
        if referer:
            headers["Referer"] = referer
        _status, _location, data = self._request_once("POST", url, body, headers, max_retries, timeout)
        return data

    def post_json(
        self, url: str, payload: dict[str, Any], max_retries: int = 5, timeout: float | None = None
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        _status, _location, data = self._request_once(
            "POST",
            url,
            body,
            {"Content-Type": "application/json", "Content-Length": str(len(body))},
            max_retries,
            timeout,
        )
        result: dict[str, Any] = json.loads(data.decode("utf-8"))
        return result
