"""离线验证JavBus重定向及浏览器验证响应的处理。"""

import pytest
from requests import Response

from javsp.datatype import MovieInfo
from javsp.web import javbus
from javsp.web.exceptions import CrawlerError, MovieNotFoundError, SiteBlocked

MOVIE_PAGE = """
<html><head><title>ABC-123</title></head><body>
<div class="container">
  <h3>ABC-123 Test title</h3>
  <a class="bigImage"><img src="/cover.jpg"></a>
  <div class="col-md-3 info">
    <p><span>識別碼:</span><span>ABC-123</span></p>
    <p><span>發行日期:</span> 2026-01-01</p>
    <p><span>長度:</span> 120分鐘</p>
  </div>
</div></body></html>
"""
NOT_FOUND_PAGE = "<html><head><title>404 Page Not Found!</title></head></html>"
VERIFY_URL = "https://www.javbus.com/doc/driver-verify?referer=https%3A%2F%2Fwww.javbus.com%2FABC-123"


def response(body, url="https://www.javbus.com/ABC-123", status=200, history=()):
    resp = Response()
    resp.status_code = status
    resp.url = url
    resp._content = body.encode("utf-8")
    resp.history = list(history)
    return resp


def parse(monkeypatch, resp):
    monkeypatch.setattr(javbus, "request_get", lambda *args, **kwargs: resp)
    movie = MovieInfo("ABC-123")
    javbus.parse_data(movie)
    return movie


@pytest.mark.parametrize("redirect_body", [None, "", " \r\n", "<html>Redirecting...</html>"])
def test_parse_final_movie_page(monkeypatch, redirect_body):
    history = [] if redirect_body is None else [response(redirect_body, status=302)]
    movie = parse(monkeypatch, response(MOVIE_PAGE, history=history))
    assert movie.dvdid == "ABC-123"
    assert movie.title == "Test title"
    assert movie.cover == "https://www.javbus.com/cover.jpg"
    assert movie.publish_date == "2026-01-01"
    assert movie.duration == "120"


def test_preserve_movie_data_in_redirect_chain(monkeypatch):
    history = [response("", status=302), response(MOVIE_PAGE, status=302)]
    movie = parse(monkeypatch, response("<html>Verify browser</html>", url=VERIFY_URL, history=history))
    assert movie.title == "Test title"


@pytest.mark.parametrize("body", ["", "<html>Verify browser</html>"])
def test_browser_verification_is_site_blocked(monkeypatch, body):
    resp = response(body, url=VERIFY_URL, history=[response("", status=302)])
    with pytest.raises(SiteBlocked, match="需要浏览器验证"):
        parse(monkeypatch, resp)


@pytest.mark.parametrize("in_redirect", [False, True])
def test_not_found_page(monkeypatch, in_redirect):
    if in_redirect:
        resp = response("", url=VERIFY_URL, history=[response(NOT_FOUND_PAGE, status=302)])
    else:
        resp = response(NOT_FOUND_PAGE)
    with pytest.raises(MovieNotFoundError):
        parse(monkeypatch, resp)


def test_empty_response_has_clear_error(monkeypatch):
    with pytest.raises(CrawlerError, match="返回空页面"):
        parse(monkeypatch, response(" \r\n"))


def test_cloudflare_challenge(monkeypatch):
    with pytest.raises(SiteBlocked, match="CloudFlare"):
        parse(monkeypatch, response("<html>Just a moment...</html>", status=403))
