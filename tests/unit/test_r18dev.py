"""R18.dev 本地转储、更新与在线补缺的离线验证。"""

import gzip
import io
import sqlite3

import pytest

from javsp.config import Cfg
from javsp.crawl.dispatcher import CrawlerExecutor
from javsp.crawl.r18dev import TABLES, lookup, prepare_database
from javsp.datatype import Movie, MovieInfo
from javsp.metadata.summarizer import MetadataMerger
from javsp.workflow.models import CrawlResult

SCHEMA = {
    "derived_video": [
        "content_id",
        "dvd_id",
        "title_ja",
        "title_en",
        "comment_ja",
        "comment_en",
        "runtime_mins",
        "release_date",
        "sample_url",
        "maker_id",
        "label_id",
        "series_id",
        "jacket_full_url",
        "jacket_thumb_url",
        "gallery_full_first",
        "gallery_full_last",
    ],
    "derived_actress": ["id", "name_kanji", "name_romaji", "image_url"],
    "derived_video_actress": ["content_id", "actress_id", "ordinality"],
    "derived_category": ["id", "name_ja", "name_en"],
    "derived_video_category": ["content_id", "category_id"],
    "derived_director": ["id", "name_kanji", "name_romaji"],
    "derived_video_director": ["content_id", "director_id"],
    "derived_maker": ["id", "name_ja", "name_en"],
    "derived_label": ["id", "name_ja", "name_en"],
    "derived_series": ["id", "name_ja", "name_en"],
}


def dump(title="本地标题", *, duplicate=False, broken=False, blank_preview=False):
    rows = {table: [] for table in TABLES}
    video = [
        "abc00123",
        "ABC-123",
        title,
        r"\N",
        "本地简介",
        r"\N",
        "90",
        "2026-01-01",
        r"\N" if blank_preview else "https://example.test/preview.mp4",
        "1",
        "1",
        "1",
        "digital/video/abc/abcpl",
        r"\N",
        "digital/video/abc/abcjp-1",
        "digital/video/abc/abcjp-2",
    ]
    rows["derived_video"].append(video)
    if duplicate:
        rows["derived_video"].append(["abc00124", *video[1:]])
    rows["derived_actress"].append(["1", "女優", r"\N", "https://example.test/actor.jpg"])
    rows["derived_video_actress"].append(["abc00123", "1", "1"])
    rows["derived_category"].append(["1", "分類", r"\N"])
    rows["derived_video_category"].append(["abc00123", "1"])
    rows["derived_director"].append(["1", "監督", r"\N"])
    rows["derived_video_director"].append(["abc00123", "1"])
    for table in ("derived_maker", "derived_label", "derived_series"):
        rows[table].append(["1", "名称", r"\N"])
    lines = []
    for table, columns in SCHEMA.items():
        lines.append(f"COPY public.{table} ({', '.join(columns)}) FROM stdin;\n")
        lines.extend("\t".join(row) + "\n" for row in rows[table])
        lines.append("\\.\n")
    if broken:
        lines.pop()
    return gzip.compress("".join(lines).encode())


class Response:
    def __init__(self, url, body, size=None):
        self.url = url
        self.raw = io.BytesIO(body)
        self.headers = {"Content-Length": str(size)} if size is not None else {}

    def raise_for_status(self):
        pass

    def close(self):
        self.raw.close()


class Session:
    def __init__(self, url, body, size=None):
        self.url, self.body, self.size = url, body, size
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return Response(self.url, self.body, self.size)


@pytest.mark.parametrize("known_size", [True, False])
def test_database_download_progress(tmp_path, capsys, known_size):
    body = dump()
    size = len(body) if known_size else None
    prepare_database(tmp_path / "r18dev.sqlite", False, session=Session(url("2026-10-06"), body, size))
    output = capsys.readouterr().err
    assert "下载 R18.dev 数据库" in output
    if known_size:
        assert "100%" in output
    else:
        assert "100%" not in output


def url(date):
    return f"https://example.test/r18dotdev_dump_{date}.sql.gz"


def test_download_update_and_keep_old_on_failure(tmp_path):
    path = tmp_path / "config/r18dev.sqlite"
    first = Session(url("2026-10-06"), dump())
    assert prepare_database(path, False, session=first) == path
    assert lookup(path, Movie("abc123")).title == "本地标题"
    assert lookup(path, Movie(cid="abc00123")).title == "本地标题"
    assert lookup(path, Movie("ABC-123")).actress_pics["女優"] == "https://example.test/actor.jpg"
    assert prepare_database(path, False, session=Session(first.url, b"invalid")) == path
    assert lookup(path, Movie("ABC-123")).title == "本地标题"
    second = Session(url("2026-10-13"), dump("更新标题"))
    assert prepare_database(path, False, session=second) == path
    assert lookup(path, Movie("ABC-123")).title == "更新标题"
    assert prepare_database(path, False, session=Session(url("2026-10-20"), dump(broken=True))) == path
    assert lookup(path, Movie("ABC-123")).title == "更新标题"
    assert not path.with_name(path.name + ".tmp").exists()


def test_existing_database_gets_indexed_entity_lookups(tmp_path):
    path = tmp_path / "config/r18dev.sqlite"
    source_url = url("2026-10-06")
    prepare_database(path, False, session=Session(source_url, dump()))
    with sqlite3.connect(path) as db:
        for table in (
            "derived_actress",
            "derived_category",
            "derived_director",
            "derived_maker",
            "derived_label",
            "derived_series",
        ):
            db.execute(f"DROP INDEX {table}_id")
        plan = db.execute("EXPLAIN QUERY PLAN SELECT name_ja FROM derived_maker WHERE id = ?", ("1",)).fetchall()
        assert any("SCAN derived_maker" in row[3] for row in plan)

    prepare_database(path, False, session=Session(source_url, b"not a dump"))
    with sqlite3.connect(path) as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT name_ja FROM derived_maker WHERE id = ?", ("1",)).fetchall()
        assert any("USING INDEX derived_maker_id" in row[3] for row in plan)
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT e.name_kanji FROM derived_video_actress AS r "
            "JOIN derived_actress AS e ON e.id = r.actress_id WHERE r.content_id = ?",
            ("abc00123",),
        ).fetchall()
        assert any("USING INDEX derived_actress_id" in row[3] for row in plan)
    assert lookup(path, Movie("ABC-123")).actress == ["女優"]


def test_first_download_failure_and_ambiguous_id(tmp_path):
    path = tmp_path / "config/r18dev.sqlite"
    bad = Session(url("2026-10-06"), b"bad gzip")
    assert prepare_database(path, True, session=bad) is None
    with pytest.raises(RuntimeError, match="在线抓取已关闭"):
        prepare_database(path, False, session=bad)
    prepare_database(path, True, session=Session(url("2026-10-06"), dump(duplicate=True)))
    assert lookup(path, Movie("ABC-123")) is None
    assert lookup(path, Movie(cid="abc00123")) is not None


def test_local_priority_online_gap_and_source_tracking(tmp_path, monkeypatch):
    path = tmp_path / "config/r18dev.sqlite"
    prepare_database(path, True, session=Session(url("2026-10-06"), dump(blank_preview=True)))
    cfg = Cfg().model_copy(deep=True)
    cfg._r18dev_db_path = path
    cfg = cfg.model_copy(
        update={
            "r18dev": cfg.r18dev.model_copy(update={"enabled": True}),
            "crawler": cfg.crawler.model_copy(update={"normalize_actress_name": False, "online_enabled": True}),
        }
    )
    calls = []

    def online(movie, config, progress):
        calls.append(movie.dvdid)
        return CrawlResult(
            {"javdb": MovieInfo(movie, title="在线标题", score=8.0, genre=["在线分类"], cover="https://example.test/online.jpg")},
            [],
            movie.dvdid,
            movie.cid,
            movie.data_src,
        )

    monkeypatch.setattr("javsp.crawl.dispatcher._crawl", online)
    movie = Movie("ABC-123")
    result = CrawlerExecutor(cfg).crawl(movie)
    merged = MetadataMerger(cfg).merge(movie, result)
    assert calls == ["ABC-123"]
    assert list(result.data) == ["r18dev", "javdb"]
    assert merged.title == "本地标题" and merged.genre == ["分類"]
    assert merged.score == 8.0 and merged.field_sources["score"] == "javdb"
    assert merged.field_sources["cover"] == "r18dev"
    assert merged.field_sources["genre"] == "r18dev"
    assert merged.cover != "https://example.test/online.jpg"
    cfg = cfg.model_copy(update={"crawler": cfg.crawler.model_copy(update={"online_enabled": False})})
    assert list(CrawlerExecutor(cfg).crawl(movie).data) == ["r18dev"]
    assert calls == ["ABC-123"]


def test_complete_local_hit_skips_online_and_miss_uses_online(tmp_path, monkeypatch):
    path = tmp_path / "config/r18dev.sqlite"
    prepare_database(path, True, session=Session(url("2026-10-06"), dump()))
    cfg = Cfg().model_copy(deep=True)
    cfg._r18dev_db_path = path
    cfg = cfg.model_copy(
        update={
            "r18dev": cfg.r18dev.model_copy(update={"enabled": True}),
            "crawler": cfg.crawler.model_copy(update={"online_enabled": True}),
        }
    )
    calls = []

    def online(movie, config, progress):
        calls.append(movie.dvdid)
        return CrawlResult({"javbus": MovieInfo(movie, title="在线标题")}, [], movie.dvdid, movie.cid, movie.data_src)

    monkeypatch.setattr("javsp.crawl.dispatcher._crawl", online)
    assert list(CrawlerExecutor(cfg).crawl(Movie("ABC-123")).data) == ["r18dev"]
    assert list(CrawlerExecutor(cfg).crawl(Movie("ABC-999")).data) == ["javbus"]
    assert calls == ["ABC-999"]


def test_fixed_database_path_survives_working_directory_change(tmp_path, monkeypatch):
    cfg = Cfg()
    expected = cfg._r18dev_db_path
    assert expected.name == "r18dev.sqlite" and expected.parent.name == "config"
    assert expected.is_absolute()
    other = tmp_path / "media"
    other.mkdir()
    monkeypatch.chdir(other)
    assert cfg._r18dev_db_path == expected
    assert "path" not in type(cfg.r18dev).model_fields


def test_disabled_sources_are_rejected():
    cfg = Cfg()
    data = cfg.model_dump()
    data["r18dev"]["enabled"] = False
    data["crawler"]["online_enabled"] = False
    with pytest.raises(ValueError, match="不能同时关闭"):
        Cfg.model_validate(data)
