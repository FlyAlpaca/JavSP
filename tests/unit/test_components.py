"""组件结果隔离、显式配置及元数据来源。"""

import sys
from types import SimpleNamespace

from javsp import dispatcher
from javsp.config import Cfg
from javsp.datatype import Movie, MovieInfo
from javsp.file import Scanner
from javsp.models import CrawlResult
from javsp.summarizer import MetadataMerger


def test_scan_results_are_independent(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "未知.mp4").write_bytes(b"video")
    cfg = Cfg().model_copy(deep=True)
    cfg = cfg.model_copy(update={"scanner": cfg.scanner.model_copy(update={"minimum_size": 0})})
    scanner = Scanner(cfg)
    result = scanner.scan(str(first))
    other = scanner.scan(str(second))
    assert len(result.unrecognized) == 1
    assert not other.unrecognized
    assert result.unrecognized is not other.unrecognized


def test_crawler_returns_identity_without_mutating_input(monkeypatch):
    cfg = SimpleNamespace(
        crawler=SimpleNamespace(selection={"cid": ["testcid"], "normal": ["testnormal"]}),
        network=SimpleNamespace(retry=1, timeout=SimpleNamespace(total_seconds=lambda: 1)),
    )

    def cid(info):
        info.title = "CID title"

    def normal(info):
        info.title = "normal title"

    for name, parser in (("testcid", cid), ("testnormal", normal)):
        monkeypatch.setitem(sys.modules, f"javsp.web.{name}", SimpleNamespace(parse_data=parser))
    movie = Movie(cid="abc00123")
    movie.dvdid, movie.data_src = "ABC-123", "cid"
    result = dispatcher.CrawlerExecutor(cfg).crawl(movie)
    assert result.dvdid is None and list(result.data) == ["testcid"]
    assert movie.dvdid == "ABC-123"


def test_merge_preserves_source_data_and_tracks_selected_cover():
    cfg = Cfg().model_copy(deep=True)
    cfg = cfg.model_copy(update={"crawler": cfg.crawler.model_copy(update={"normalize_actress_name": False})})
    info = MovieInfo("ABC-123", title="title", cover="cover", genre=["original"])
    result = CrawlResult({"javdb": info}, [], "ABC-123", None, "normal")
    movie = Movie("ABC-123")
    merged = MetadataMerger(cfg).merge(movie, result)
    assert merged.field_sources["genre"] == "javdb"
    assert merged.field_sources["cover"] == "javdb"
    assert info.genre == ["original"] and info.genre_norm is None
    assert movie.info is None
