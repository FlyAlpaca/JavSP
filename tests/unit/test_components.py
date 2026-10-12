"""组件结果隔离、显式配置及元数据来源。"""

from types import SimpleNamespace

import pytest

from javsp.config import load_config
from javsp.crawl import dispatcher
from javsp.datatype import Movie, MovieInfo
from javsp.metadata.summarizer import MetadataMerger
from javsp.scan.file import Scanner
from javsp.workflow.models import CrawlResult


def test_scan_results_are_independent(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "未知.mp4").write_bytes(b"video")
    cfg = load_config().model_copy(deep=True)
    cfg = cfg.model_copy(update={"scanner": cfg.scanner.model_copy(update={"minimum_size": 0})})
    scanner = Scanner(cfg)
    result = scanner.scan(str(first))
    other = scanner.scan(str(second))
    assert len(result.unrecognized) == 1
    assert not other.unrecognized
    assert result.unrecognized is not other.unrecognized


def test_crawler_returns_identity_without_mutating_input(monkeypatch):
    cfg = load_config([])
    runtime = SimpleNamespace(database_path="test.sqlite")
    movie = Movie(cid="abc00123")
    movie.dvdid, movie.data_src = "ABC-123", "cid"
    source = MovieInfo(cid="abc00123", title="CID title")
    monkeypatch.setattr(dispatcher, "lookup", lambda path, candidate: source)
    result = dispatcher.CrawlerExecutor(cfg, runtime=runtime).crawl(movie)
    assert result.dvdid == "ABC-123" and list(result.data) == ["r18dev"]
    assert movie.dvdid == "ABC-123" and movie.info is None


def test_merge_preserves_source_data_and_tracks_selected_cover():
    cfg = load_config().model_copy(deep=True)
    cfg = cfg.model_copy(update={"r18dev": cfg.r18dev.model_copy(update={"normalize_actress_name": False})})
    info = MovieInfo("ABC-123", title="title", cover="cover", genre=["original"])
    result = CrawlResult({"r18dev": info}, [], "ABC-123", None, "normal")
    movie = Movie("ABC-123")
    merged = MetadataMerger(cfg).merge(movie, result)
    assert merged.field_sources["genre"] == "r18dev"
    assert merged.field_sources["cover"] == "r18dev"
    assert info.genre == ["original"] and info.genre_norm is None
    assert movie.info is None


def test_scanner_uses_supplied_id_patterns(tmp_path):
    (tmp_path / "XYZ-999 ABC-123.mp4").write_bytes(b"video")
    cfg = load_config()
    scanner = cfg.scanner.model_copy(update={"minimum_size": 0, "ignored_id_pattern": ["XYZ-999"]})
    cfg = cfg.model_copy(update={"scanner": scanner})
    result = Scanner(cfg).scan(str(tmp_path))
    assert result.movies[0].dvdid == "ABC-123"


def test_empty_folder_ignore_list_scans_subdirectories(tmp_path):
    folder = tmp_path / 'movie'
    folder.mkdir()
    (folder / 'ABC-123.mp4').write_bytes(b'video')
    cfg = load_config()
    cfg = cfg.model_copy(update={'scanner': cfg.scanner.model_copy(update={
        'minimum_size': 0, 'ignored_folder_name_pattern': [], 'skip_nfo_dir': False,
    })})
    result = Scanner(cfg).scan(str(tmp_path))
    assert [movie.dvdid for movie in result.movies] == ['ABC-123']
    assert result.skipped == []


@pytest.mark.parametrize('respect', [True, False])
@pytest.mark.parametrize('cid_only', [True, False])
def test_local_source_identity_rule_preserves_input(respect, cid_only):
    cfg = load_config([])
    cfg = cfg.model_copy(update={'r18dev': cfg.r18dev.model_copy(update={'respect_source_id': respect})})
    movie = Movie(cid='abc123') if cid_only else Movie('ABC-123')
    info = MovieInfo(dvdid='ABC-00123', cid='abc00123', title='title', cover='cover', genre=['分類'])
    result = CrawlResult({'r18dev': info}, [], movie.dvdid, movie.cid, movie.data_src)
    merged = MetadataMerger(cfg).merge(movie, result)
    assert merged.dvdid == ('ABC-00123' if respect and not cid_only else movie.dvdid)
    assert merged.cid == ('abc00123' if cid_only and respect else movie.cid)
    assert info.dvdid == 'ABC-00123' and info.genre == ['分類']
    assert movie.info is None
