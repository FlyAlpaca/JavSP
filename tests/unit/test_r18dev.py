"""R18.dev 本地转储、更新与在线补缺的离线验证。"""

import gzip
import hashlib
import re
import sqlite3
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from javsp.config import Cfg
from javsp.crawl import r18dev
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


_server_state = None


@pytest.fixture(autouse=True)
def wasabi_server(monkeypatch):
    global _server_state
    state = SimpleNamespace(
        objects={},
        requests=[],
        page_size=1000,
        list_error=False,
        ignore_range=False,
        probe_ignored=False,
        bad_range=False,
        fail_once=False,
        change_after_probe=False,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/":
                if state.list_error:
                    self.send_error(503)
                    return
                params = parse_qs(parsed.query)
                keys = sorted(key for key in state.objects if key.startswith(params.get("prefix", [""])[0]))
                token = params.get("continuation-token", [None])[0]
                if token:
                    keys = [key for key in keys if key > token]
                page = keys[: state.page_size]
                ns = "http://s3.amazonaws.com/doc/2006-03-01/"
                root = ET.Element(f"{{{ns}}}ListBucketResult")
                ET.SubElement(root, f"{{{ns}}}IsTruncated").text = "true" if len(keys) > len(page) else "false"
                if len(keys) > len(page):
                    ET.SubElement(root, f"{{{ns}}}NextContinuationToken").text = page[-1]
                for key in page:
                    body = state.objects[key]
                    entry = ET.SubElement(root, f"{{{ns}}}Contents")
                    ET.SubElement(entry, f"{{{ns}}}Key").text = key
                    ET.SubElement(entry, f"{{{ns}}}Size").text = str(len(body))
                    ET.SubElement(entry, f"{{{ns}}}ETag").text = '"' + hashlib.sha256(body).hexdigest() + '"'
                payload = ET.tostring(root)
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            key = parsed.path.lstrip("/")
            if key not in state.objects:
                self.send_error(404)
                return
            body = state.objects[key]
            etag = '"' + hashlib.sha256(body).hexdigest() + '"'
            requested = self.headers.get("Range")
            state.requests.append((key, requested, self.headers.get("If-Match")))
            if self.headers.get("If-Match") != etag:
                self.send_response(412)
                self.end_headers()
                return
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", requested or "")
            if match and not state.ignore_range and not (state.probe_ignored and requested == "bytes=0-0"):
                start, end = map(int, match.groups())
                payload = body[start : end + 1]
                self.send_response(206)
                range_start = 0 if state.bad_range and requested != "bytes=0-0" else start
                self.send_header("Content-Range", f"bytes {range_start}-{end}/{len(body)}")
            else:
                payload = body
                self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("ETag", etag)
            self.end_headers()
            if state.change_after_probe and requested == "bytes=0-0":
                state.change_after_probe = False
                state.objects[key] = body + b"changed"
            if state.fail_once and requested != "bytes=0-0":
                state.fail_once = False
                self.wfile.write(payload[: max(1, len(payload) // 2)])
                self.wfile.flush()
                self.close_connection = True
            else:
                self.wfile.write(payload)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    state.base = f"http://127.0.0.1:{server.server_port}/"
    monkeypatch.setattr(r18dev, "BUCKET_URL", state.base)
    _server_state = state
    yield state
    _server_state = None
    server.shutdown()
    thread.join()
    server.server_close()


class Session(requests.Session):
    def __init__(self, source_url, body):
        super().__init__()
        self.url = source_url
        self.calls = 0
        _server_state.objects = {urlparse(source_url).path.lstrip("/"): body}

    def get(self, *args, **kwargs):
        self.calls += 1
        return super().get(*args, **kwargs)


def test_database_download_progress(tmp_path, caplog):
    caplog.set_level("INFO", logger="javsp.crawl.r18dev")
    body = dump()
    prepare_database(tmp_path / "r18dev.sqlite", False, session=Session(url("2026-10-06"), body))
    assert "下载 R18.dev 数据库: 100%" in caplog.text


def url(date):
    return f"{_server_state.base}dumps/r18dotdev_dump_{date}.sql.gz"


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


def test_list_pagination_and_newest_valid_date(tmp_path, wasabi_server):
    path = tmp_path / "r18dev.sqlite"
    session = Session(url("2026-10-06"), dump("最新标题"))
    wasabi_server.objects[urlparse(url("2026-09-29")).path.lstrip("/")] = dump("旧标题")
    wasabi_server.objects["dumps/r18dotdev_dump_2026-99-99.sql.gz"] = dump("无效日期")
    wasabi_server.objects["dumps/r18dotdev_dump_2026-10-20.sql.gz"] = b""
    wasabi_server.page_size = 1
    assert prepare_database(path, False, session=session) == path
    assert lookup(path, Movie("ABC-123")).title == "最新标题"
    assert session.calls >= 3


def test_same_date_etag_change_and_no_downgrade(tmp_path, wasabi_server):
    path = tmp_path / "r18dev.sqlite"
    source_url = url("2026-10-06")
    first = Session(source_url, dump("初版"))
    prepare_database(path, False, session=first)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT dump_date, etag, size FROM dump_meta").fetchone()[0] == "2026-10-06"
    unchanged = Session(source_url, dump("初版"))
    before = len(wasabi_server.requests)
    prepare_database(path, False, session=unchanged)
    assert len(wasabi_server.requests) == before
    assert unchanged.calls == 1
    changed = Session(source_url, dump("修订版"))
    prepare_database(path, False, session=changed)
    assert lookup(path, Movie("ABC-123")).title == "修订版"
    older = Session(url("2026-09-29"), dump("旧日期"))
    prepare_database(path, False, session=older)
    assert older.calls == 1
    assert lookup(path, Movie("ABC-123")).title == "修订版"


def test_legacy_database_reimports_same_date_once(tmp_path):
    path = tmp_path / "r18dev.sqlite"
    source_url = url("2026-10-06")
    prepare_database(path, False, session=Session(source_url, dump("旧版")))
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE dump_meta RENAME TO old_dump_meta")
        db.execute("CREATE TABLE dump_meta (source_url TEXT NOT NULL)")
        db.execute("INSERT INTO dump_meta SELECT source_url FROM old_dump_meta")
        db.execute("DROP TABLE old_dump_meta")
    upgraded = Session(source_url, dump("同日新内容"))
    prepare_database(path, False, session=upgraded)
    assert upgraded.calls > 1
    assert lookup(path, Movie("ABC-123")).title == "同日新内容"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT dump_date, etag, size FROM dump_meta").fetchone()[0] == "2026-10-06"


def test_database_resumes_partial_segment(tmp_path, wasabi_server, monkeypatch):
    path = tmp_path / "r18dev.sqlite"
    session = Session(url("2026-10-06"), dump())
    monkeypatch.setattr("javsp.download.CHUNK_SIZE", 16)
    wasabi_server.fail_once = True
    assert prepare_database(path, True, session=session) is None
    assert any(part.stat().st_size for part in tmp_path.glob("*.part*"))
    before = len(wasabi_server.requests)
    prepare_database(path, False, session=session)
    starts = {i * len(dump()) // 4 for i in range(4)}
    assert any(
        request and request != "bytes=0-0" and int(request[6:].split("-")[0]) not in starts
        for _, request, _ in wasabi_server.requests[before:]
    )
    assert not list(tmp_path.glob("*.part*"))


def test_database_single_connection_resume(tmp_path, wasabi_server, monkeypatch):
    path = tmp_path / "r18dev.sqlite"
    session = Session(url("2026-10-06"), dump())
    monkeypatch.setattr("javsp.download.CHUNK_SIZE", 16)
    wasabi_server.probe_ignored = True
    wasabi_server.fail_once = True
    assert prepare_database(path, True, session=session) is None
    before = len(wasabi_server.requests)
    prepare_database(path, False, session=session)
    assert any(request and request != "bytes=0-0" for _, request, _ in wasabi_server.requests[before:])
    assert not list(tmp_path.glob("*.part*"))


@pytest.mark.parametrize("mode", ["ignore_range", "bad_range"])
def test_database_range_fallback(tmp_path, wasabi_server, mode):
    session = Session(url("2026-10-06"), dump())
    setattr(wasabi_server, mode, True)
    path = tmp_path / "r18dev.sqlite"
    assert prepare_database(path, False, session=session) == path
    assert any(request is None for _, request, _ in wasabi_server.requests)


def test_object_changes_during_download_preserves_old(tmp_path, wasabi_server):
    path = tmp_path / "r18dev.sqlite"
    prepare_database(path, False, session=Session(url("2026-10-06"), dump("旧版")))
    session = Session(url("2026-10-13"), dump("新版"))
    wasabi_server.change_after_probe = True
    assert prepare_database(path, False, session=session) == path
    assert lookup(path, Movie("ABC-123")).title == "旧版"
    assert not list(tmp_path.glob("*.part*"))


def test_listing_failure_preserves_old(tmp_path, wasabi_server):
    path = tmp_path / "r18dev.sqlite"
    prepare_database(path, False, session=Session(url("2026-10-06"), dump()))
    session = Session(url("2026-10-13"), dump("新版"))
    wasabi_server.list_error = True
    assert prepare_database(path, False, session=session) == path
    assert lookup(path, Movie("ABC-123")).title == "本地标题"


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
