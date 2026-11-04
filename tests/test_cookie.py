from __future__ import annotations

import pytest

from thatrobot2api.upstream import CookieError, estimate_tokens, load_session

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    "# https://curl.se/docs/http-cookies.html\n"
    "\n"
    "#HttpOnly_platform.thatrobot.ai\tFALSE\t/\tTRUE\t1793823022\ttr_session\teyJjwt.value.sig\n"
)


def test_reads_raw_jwt(tmp_path):
    path = tmp_path / "cookie.txt"
    path.write_text("eyJraw.token.sig\n")
    assert load_session(path) == "eyJraw.token.sig"


def test_reads_netscape_cookie_file(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(NETSCAPE)
    assert load_session(path) == "eyJjwt.value.sig"


def test_space_separated_line(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text("platform.thatrobot.ai\tFALSE\t/\tTRUE\t1793823022\ttr_session eyJspace.sig\n")
    assert load_session(path) == "eyJspace.sig"


def test_missing_cookie_raises(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(NETSCAPE.replace("tr_session", "other"))
    with pytest.raises(CookieError):
        load_session(path)


def test_estimate_tokens():
    assert estimate_tokens(0) == 0
    assert estimate_tokens(1) == 1
    assert estimate_tokens(8) == 2
    assert estimate_tokens(9) == 3


PRIVATE_URL_CASES = [
    ("see http://127.0.0.1:9222/x", "see 127.0.0.1:9222/x"),
    ("http://localhost:8080/v1", "localhost:8080/v1"),
    ("wss://127.0.0.1/ws", "127.0.0.1/ws"),
    ("http://10.0.0.5/x", "10.0.0.5/x"),
    ("http://192.168.1.10", "192.168.1.10"),
    ("http://172.16.4.2/a", "172.16.4.2/a"),
    ("endpoint (http://127.0.0.1:9222) ready", "endpoint (127.0.0.1:9222) ready"),
    ("http://example.com/x", "http://example.com/x"),
    ("http://127.0.0.1.example.com", "http://127.0.0.1.example.com"),
    ("127.0.0.1:9222", "127.0.0.1:9222"),
    ("https://api.192.168.1.1.example.com", "https://api.192.168.1.1.example.com"),
]


@pytest.mark.parametrize("source,expected", PRIVATE_URL_CASES)
def test_rewrite_private_urls(source, expected):
    from thatrobot2api.upstream import rewrite_private_urls

    sanitized, hits = rewrite_private_urls([{"role": "user", "content": source}])
    assert sanitized[0]["content"] == expected
    assert hits == (0 if source == expected else 1)
