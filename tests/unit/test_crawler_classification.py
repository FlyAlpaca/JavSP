"""影片分类不随标签顺序改变。"""

import pytest
from lxml import html

from javsp.datatype import MovieInfo
from javsp.web import javbus, javdb
from tests.unit.test_javbus_redirect import MOVIE_PAGE, response


@pytest.mark.parametrize(
    "links, expected",
    [
        (["/uncensored/genre/1", "/genre/2"], True),
        (["/genre/2", "/uncensored/genre/1"], True),
        (["/genre/2"], False),
        ([], None),
    ],
)
def test_javbus_classification(monkeypatch, links, expected):
    tags = "".join(f'<span class="genre"><label><a href="{link}">Tag</a></label></span>' for link in links)
    body = MOVIE_PAGE.replace("<p><span>識別碼:", tags + "<p><span>識別碼:")
    monkeypatch.setattr(javbus, "request_get", lambda *a, **kw: response(body))
    movie = MovieInfo("ABC-123")
    javbus.parse_data(movie)
    assert movie.uncensored is expected


@pytest.mark.parametrize(
    "links, expected",
    [
        (["/uncensored?x=1", "/genres/2"], True),
        (["/genres/2", "/uncensored?x=1"], True),
        (["/tags?x=1", "/genres/2"], False),
        (["/genres/2", "/tags?x=1"], False),
        (["/tags?x=1", "/uncensored?x=1"], True),
        (["/genres/2"], None),
        ([], None),
    ],
)
def test_javdb_classification(monkeypatch, links, expected):
    search = html.fromstring('<a class="box" href="/v/1"><div class="video-title"><strong>ABC-123</strong></div></a>')
    tags = "".join(f'<a href="{link}">Tag</a>' for link in links)
    detail = html.fromstring(f"""<html><body><section><div><div class="video-detail">
    <h2><strong class="current-title">ABC-123 Title</strong></h2><img class="video-cover" src="cover.jpg">
    <nav class="panel movie-panel-info"><div><span>ABC-123</span></div>
    <div><strong>日期:</strong><span>2026-01-01</span></div>
    <div><strong>時長:</strong><span>120分鍾</span></div>
    <div><strong>類別:</strong><span>{tags}</span></div>
    <div><strong>演員:</strong><span><a>Actress</a><strong>♀</strong></span></div>
    </nav></div></div></section></body></html>""")
    monkeypatch.setattr(javdb, "get_html_wrapper", lambda url: search if "/search?" in url else detail)
    movie = MovieInfo("ABC-123")
    javdb.parse_data(movie)
    assert movie.uncensored is expected
