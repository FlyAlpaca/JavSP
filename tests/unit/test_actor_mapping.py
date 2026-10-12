"""演员名称格式、别名开关与 GitHub 缓存更新。"""

import json

import pytest
import requests
from lxml import etree

from javsp import startup
from javsp.config import load_config
from javsp.datatype import Movie, MovieInfo
from javsp.metadata import actors, summarizer
from javsp.metadata.actors import ActorMapping, ActorMappingError, prepare_actor_mapping
from javsp.metadata.summarizer import MetadataMerger
from javsp.publish.naming import generate_names
from javsp.publish.nfo import write_nfo
from javsp.workflow.models import CrawlResult

XML = """<?xml version="1.0" encoding="UTF-8"?>
<actor-mapping><actor>
  <a jp="新ありな" zh_cn="新有菜" zh_tw="新有菜" keyword="橋本ありな,橋本有菜,共用别名" />
  <a jp="葵つかさ" zh_cn="" zh_tw="葵司" keyword="共用别名" />
  <a jp="JULIA" zh_cn="" keyword="京香じゅりあ" />
  <a jp="波多野結衣" zh_cn="波多野結衣" keyword="酒井愛美" />
  <a jp="" zh_cn="缺少原名" keyword="不可推断" />
</actor></actor-mapping>""".encode()


class Response:
    def __init__(self, body=XML, status=200, etag='"v1"', error=None):
        self.body, self.status_code, self.error = body, status, error
        self.headers = {"ETag": etag, "Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, _size):
        yield self.body[:30]
        if self.error:
            raise self.error
        yield self.body[30:]


class Session:
    def __init__(self, response=None, error=None, head_response=None):
        self.response = response or Response()
        self.head_response = head_response or self.response
        self.error = error
        self.calls = []
        self.head_calls = []

    def head(self, url, **options):
        self.head_calls.append((url, options))
        if self.error:
            raise self.error
        return self.head_response

    def get(self, url, **options):
        self.calls.append((url, options))
        if self.error:
            raise self.error
        return self.response


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("新ありな", "新ありな（新有菜）"),
        ("新有菜", "新ありな（新有菜）"),
        ("橋本ありな", "新ありな（新有菜）"),
        ("葵つかさ", "葵つかさ（葵司）"),
        ("JULIA", "JULIA"),
        ("波多野結衣", "波多野結衣"),
        ("缺少原名", "缺少原名"),
        ("共用别名", "共用别名"),
        ("未知女优", "未知女优"),
    ],
)
def test_names_and_fallbacks(name, expected):
    mapping = ActorMapping.from_xml(XML)
    assert mapping.resolve(name) == expected
    assert mapping.resolve(expected) == expected


def test_alias_switch_preserves_alias_but_keeps_name_format():
    mapping = ActorMapping.from_xml(XML)
    assert mapping.resolve("新ありな", normalize_aliases=False) == "新ありな（新有菜）"
    assert mapping.resolve("新有菜", normalize_aliases=False) == "新ありな（新有菜）"
    assert mapping.resolve("橋本ありな", normalize_aliases=False) == "橋本ありな（新有菜）"
    assert mapping.resolve("橋本ありな（新有菜）", normalize_aliases=False) == "橋本ありな（新有菜）"
    assert mapping.resolve("京香じゅりあ", normalize_aliases=False) == "京香じゅりあ"
    assert mapping.resolve("共用别名", normalize_aliases=False) == "共用别名"


def test_duplicate_entries_and_japanese_primary_precedence():
    mapping = ActorMapping.from_xml(
        """<actor-mapping><actor>
      <a jp="桃乃木香奈" zh_cn="桃乃木香奈" keyword="桃乃木かな,松嶋真麻" />
      <a jp="桃乃木かな" zh_cn="桃乃木香奈" keyword="松嶋真麻" />
      <a jp="星奈あい" zh_cn="星奈爱" keyword="旧名" />
      <a jp="星奈あい" zh_cn="星奈あい" keyword="旧名" />
    </actor></actor-mapping>""".encode()
    )
    assert mapping.resolve("桃乃木かな") == "桃乃木かな（桃乃木香奈）"
    assert mapping.resolve("松嶋真麻") == "松嶋真麻"
    assert mapping.resolve("星奈あい") == "星奈あい"
    assert mapping.resolve("旧名") == "旧名"


@pytest.mark.parametrize(
    "body", [b"partial xml", b"<html/>", b"<actor-mapping/>", b'<!DOCTYPE actor-mapping [<!ENTITY x "name">]><actor-mapping/>']
)
def test_invalid_xml_rejected(body):
    with pytest.raises(ActorMappingError):
        ActorMapping.from_xml(body)


def test_cache_update_and_conditional_check(tmp_path):
    path = tmp_path / "config/actor-mapping.xml"
    first = Session()
    assert prepare_actor_mapping(path, session=first).resolve("新有菜") == "新ありな（新有菜）"
    assert path.read_bytes() == XML
    assert "If-None-Match" not in first.calls[0][1]["headers"]
    stamp = path.stat().st_mtime_ns
    unchanged = Session(Response(status=304))
    assert prepare_actor_mapping(path, session=unchanged).resolve("橋本ありな") == "新ありな（新有菜）"
    assert unchanged.head_calls[0][1]["headers"]["If-None-Match"] == '"v1"'
    assert not unchanged.calls
    assert path.stat().st_mtime_ns == stamp
    new_body = XML.replace("新有菜".encode(), "新译名".encode())
    updated = Session(Response(new_body, etag='"v2"'))
    assert prepare_actor_mapping(path, session=updated).resolve("新ありな") == "新ありな（新译名）"
    assert path.read_bytes() == new_body
    assert len(updated.head_calls) == 1 and len(updated.calls) == 1
    assert json.loads(path.with_suffix(".json").read_text())["etag"] == '"v2"'
    assert not list(path.parent.glob(".actor-mapping-*"))


@pytest.mark.parametrize("status", [200, 304])
def test_matching_head_version_never_downloads_or_rewrites_cache(tmp_path, status):
    path = tmp_path / "actor-mapping.xml"
    prepare_actor_mapping(path, session=Session())
    metadata_path = path.with_suffix(".json")
    before = (path.stat().st_mtime_ns, metadata_path.stat().st_mtime_ns)
    session = Session(Response(status=status))
    assert prepare_actor_mapping(path, session=session, proxy="http://localhost:8080").row_count == 4
    assert len(session.head_calls) == 1 and not session.calls
    options = session.head_calls[0][1]
    assert "Range" not in options["headers"]
    assert options["headers"]["Accept-Encoding"] == "identity"
    assert options["proxies"] == {"http": "http://localhost:8080", "https": "http://localhost:8080"}
    assert (path.stat().st_mtime_ns, metadata_path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("head_status", [200, 405, 501])
@pytest.mark.parametrize("get_status", [200, 206, 304])
def test_head_without_validator_falls_back_to_get_and_skips_unchanged_body(tmp_path, head_status, get_status):
    path = tmp_path / "actor-mapping.xml"
    prepare_actor_mapping(path, session=Session())
    head = Response(status=head_status)
    head.headers.pop("ETag")
    response = Response(status=get_status)

    def no_read(_size):
        pytest.fail("未更新的映射不应读取响应正文")

    response.iter_content = no_read
    session = Session(response, head_response=head)
    assert prepare_actor_mapping(path, session=session).row_count == 4
    assert len(session.head_calls) == 1 and len(session.calls) == 1
    assert path.read_bytes() == XML


@pytest.mark.parametrize("status", [405, 501])
def test_unsupported_head_still_downloads_changed_mapping(tmp_path, status):
    path = tmp_path / "actor-mapping.xml"
    prepare_actor_mapping(path, session=Session())
    body = XML.replace("新有菜".encode(), "新译名".encode())
    session = Session(Response(body, etag='"v2"'), head_response=Response(status=status))
    assert prepare_actor_mapping(path, session=session).resolve("新ありな") == "新ありな（新译名）"
    assert path.read_bytes() == body


def test_head_timeout_retries_without_downloading_unchanged_mapping(tmp_path):
    path = tmp_path / "actor-mapping.xml"
    prepare_actor_mapping(path, session=Session())
    session = Session()
    real_head = session.head
    attempts = []

    def flaky_head(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise requests.ReadTimeout("temporary timeout")
        return real_head(*args, **kwargs)

    session.head = flaky_head
    assert prepare_actor_mapping(path, session=session).row_count == 4
    assert len(attempts) == 2 and not session.calls


@pytest.mark.parametrize(
    "response", [Response(b"<html/>", etag='"v2"'), Response(status=503),
                 Response(etag='"v2"', error=requests.ConnectionError("interrupted"))]
)
def test_failed_update_preserves_valid_cache(tmp_path, response, caplog):
    path = tmp_path / "actor-mapping.xml"
    prepare_actor_mapping(path, session=Session())
    assert prepare_actor_mapping(path, session=Session(response)).resolve("新有菜") == "新ありな（新有菜）"
    assert path.read_bytes() == XML
    assert "继续使用有效缓存" in caplog.text


def test_network_failure_requires_valid_cache(tmp_path):
    path = tmp_path / "actor-mapping.xml"
    offline = Session(error=requests.ConnectionError("offline"))
    with pytest.raises(ActorMappingError, match="无有效缓存"):
        prepare_actor_mapping(path, session=offline)
    prepare_actor_mapping(path, session=Session())
    assert prepare_actor_mapping(path, session=offline).resolve("新有菜") == "新ありな（新有菜）"
    path.write_bytes(b"broken")
    with pytest.raises(ActorMappingError, match="无有效缓存"):
        prepare_actor_mapping(path, session=offline)


def test_corrupt_cache_or_version_metadata_forces_full_request(tmp_path):
    path = tmp_path / "actor-mapping.xml"
    prepare_actor_mapping(path, session=Session())
    path.write_bytes(b"broken")
    repair = Session()
    prepare_actor_mapping(path, session=repair)
    assert "If-None-Match" not in repair.calls[0][1]["headers"]
    assert not repair.head_calls
    metadata = json.loads(path.with_suffix(".json").read_text())
    metadata["sha256"] = "mismatch"
    path.with_suffix(".json").write_text(json.dumps(metadata))
    refetch = Session()
    prepare_actor_mapping(path, session=refetch)
    assert "If-None-Match" not in refetch.calls[0][1]["headers"]
    assert not refetch.head_calls


def test_missing_version_metadata_requires_download(tmp_path):
    path = tmp_path / "actor-mapping.xml"
    path.write_bytes(XML)
    session = Session()
    assert prepare_actor_mapping(path, session=session).row_count == 4
    assert not session.head_calls and len(session.calls) == 1
    assert "If-None-Match" not in session.calls[0][1]["headers"]
    assert path.with_suffix(".json").is_file()


def test_atomic_publication_failure_keeps_old_mapping(tmp_path, monkeypatch):
    path = tmp_path / "actor-mapping.xml"
    prepare_actor_mapping(path, session=Session())

    def denied(*_args):
        raise OSError("replace denied")

    monkeypatch.setattr(actors.os, "replace", denied)
    new_body = XML.replace("新有菜".encode(), "新译名".encode())
    result = prepare_actor_mapping(path, session=Session(Response(new_body, etag='"v2"')))
    assert result.resolve("新ありな") == "新ありな（新有菜）"
    assert path.read_bytes() == XML
    assert not list(tmp_path.glob(".actor-mapping-*"))


def test_size_and_download_time_limits_preserve_cache(tmp_path, monkeypatch):
    path = tmp_path / "actor-mapping.xml"
    prepare_actor_mapping(path, session=Session())
    large = Response(etag='"v2"')
    large.headers["Content-Length"] = str(actors.MAX_MAPPING_SIZE + 1)
    prepare_actor_mapping(path, session=Session(large))
    clock = iter([0, actors.DOWNLOAD_DEADLINE + 1])
    monkeypatch.setattr(actors.time, "monotonic", lambda: next(clock))
    prepare_actor_mapping(path, session=Session(Response(etag='"v2"')))
    assert path.read_bytes() == XML


def test_startup_loads_snapshot_even_with_alias_switch_off(tmp_path, monkeypatch):
    cfg = load_config()
    cfg = cfg.model_copy(update={"scanner": cfg.scanner.model_copy(update={"input_directory": tmp_path}),
                                 "crawler": cfg.crawler.model_copy(update={"normalize_actress_name": False})})
    monkeypatch.setattr(startup, "check_for_updates", lambda: None)
    monkeypatch.setattr(startup, "import_crawlers", lambda cfg: None)
    monkeypatch.setattr(startup, "prepare_database", lambda *args, **kw: None)
    session = Session()
    monkeypatch.setattr(startup, "prepare_actor_mapping",
                        lambda path, proxy: prepare_actor_mapping(path, session=session, proxy=proxy))
    runtime = startup.prepare_startup(cfg, base_dir=tmp_path)
    mapping = runtime.actor_mapping
    assert runtime.actor_mapping_path == tmp_path / "config/actor-mapping.xml"
    monkeypatch.chdir(tmp_path.parent)
    assert runtime.actor_mapping_path == tmp_path / "config/actor-mapping.xml"
    assert MetadataMerger(cfg, runtime=runtime).aliases is mapping


def test_alias_helper_and_merger_use_explicit_snapshot():
    mapping = ActorMapping.from_xml(XML)
    assert summarizer.resolve_alias("橋本ありな", mapping) == "新ありな（新有菜）"
    movie = Movie("ABC-123")
    source = MovieInfo(movie, title="title", cover="cover", actress=["新ありな"])
    result = CrawlResult({"r18dev": source}, [], movie.dvdid, None, "normal")
    merged = MetadataMerger(load_config(), aliases=mapping).merge(movie, result)
    assert merged.actress == ["新ありな（新有菜）"]
    assert source.actress == ["新ありな"]


@pytest.mark.parametrize("normalize", [True, False])
def test_merged_names_consistent_in_paths_nfo_and_thumbnails(tmp_path, normalize):
    cfg = load_config().model_copy(deep=True)
    mapping = ActorMapping.from_xml(XML)
    cfg = cfg.model_copy(update={"crawler": cfg.crawler.model_copy(update={"normalize_actress_name": normalize})})
    movie = Movie("ABC-123")
    movie.files = [str(tmp_path / "ABC-123.mp4")]
    source = MovieInfo(
        movie,
        title="影片 新ありな",
        cover="cover",
        actress=["新ありな", "新有菜", "橋本ありな"],
        actress_pics={"新有菜": "portrait", "橋本ありな": "other"},
    )
    merged = MetadataMerger(cfg, aliases=mapping).merge(movie, CrawlResult({"r18dev": source}, [], "ABC-123", None, "normal"))
    display = "新ありな（新有菜）"
    expected = [display] if normalize else [display, "橋本ありな（新有菜）"]
    assert merged.actress == expected
    assert merged.actress_pics[display] == "portrait"
    assert source.actress == ["新ありな", "新有菜", "橋本ありな"]
    assert source.title == "影片 新ありな" and merged.title == "影片"
    movie.info = merged
    generate_names(movie, cfg)
    assert ",".join(expected) in movie.save_dir
    assert merged.get_info_dic(cfg)["actress"] == ",".join(expected)
    target = tmp_path / "movie.nfo"
    write_nfo(merged, target, cfg)
    tree = etree.parse(str(target))
    assert tree.xpath("/movie/actor/name/text()") == expected
    assert tree.xpath("/movie/actor[name=$name]/thumb/text()", name=display) == ["portrait"]


def test_component_requires_explicit_mapping_for_names(tmp_path):
    cfg = load_config()
    movie = Movie("ABC-123")
    result = CrawlResult(
        {"r18dev": MovieInfo(movie, title="title", cover="cover", actress=["新ありな"])}, [], "ABC-123", None, "normal"
    )
    with pytest.raises(ValueError, match="演员映射未初始化"):
        MetadataMerger(cfg).merge(movie, result)
    path = tmp_path / "actor-mapping.xml"
    path.write_bytes(XML)
    merger = MetadataMerger(cfg, aliases=ActorMapping.from_file(path))
    assert merger.merge(movie, result).actress == ["新ありな（新有菜）"]


@pytest.fixture
def mapping_server(monkeypatch):
    """本地真实 HTTP 范围响应，验证分片线程与中断续传。"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from javsp import download

    state = {"body": XML, "etag": '"test-v1"', "ranges": [], "heads": [], "fail": False, "ignore_range": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_HEAD(self):
            state["heads"].append(self.headers.get("If-None-Match"))
            matched = self.headers.get("If-None-Match") == state["etag"]
            self.send_response(304 if matched and not state.get("ignore_conditional") else 200)
            self.send_header("ETag", state["etag"])
            self.send_header("Content-Length", str(len(state["body"])))
            self.end_headers()

        def do_GET(self):
            body = state["body"]
            if self.headers.get("If-None-Match") == state["etag"]:
                self.send_response(304)
                self.end_headers()
                return
            if state.get("change_during_download") and self.headers.get("If-Match"):
                state["etag"] = '"test-v2"'
                state["change_during_download"] = False
            if self.headers.get("If-Match") and self.headers["If-Match"] != state["etag"]:
                self.send_response(412)
                self.end_headers()
                return
            requested = self.headers.get("Range")
            state["ranges"].append(requested)
            start, end = 0, len(body) - 1
            ranged = requested and not state["ignore_range"]
            if ranged:
                start, end = map(int, requested.removeprefix("bytes=").split("-"))
            self.send_response(206 if ranged else 200)
            self.send_header("ETag", state["etag"])
            self.send_header("Content-Length", str(end - start + 1))
            if ranged:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
            self.end_headers()
            content = body[start:end + 1]
            if state["fail"] and end > start:
                self.wfile.write(content[:64])
                self.wfile.flush()
                self.close_connection = True
            else:
                self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setattr(actors, "MAPPING_URL", f"http://127.0.0.1:{server.server_port}/mapping.xml")
    monkeypatch.setattr(actors, "MAPPING_PART_SIZE", 128)
    monkeypatch.setattr(download, "CHUNK_SIZE", 64)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_mapping_parallel_download_and_conditional_cache(tmp_path, mapping_server):
    path = tmp_path / "actor-mapping.xml"
    assert prepare_actor_mapping(path).row_count == 4
    assert path.read_bytes() == XML
    assert "bytes=128-255" in mapping_server["ranges"]
    assert not list(tmp_path.glob(".*-download-*"))
    before = path.stat().st_mtime_ns
    ranges = list(mapping_server["ranges"])
    assert prepare_actor_mapping(path).row_count == 4
    assert path.stat().st_mtime_ns == before
    assert mapping_server["heads"] == ['"test-v1"']
    assert mapping_server["ranges"] == ranges


def test_mapping_server_ignores_conditional_head_but_matching_etag_skips_get(tmp_path, mapping_server):
    path = tmp_path / "actor-mapping.xml"
    prepare_actor_mapping(path)
    mapping_server["ignore_conditional"] = True
    mapping_server["ranges"].clear()
    assert prepare_actor_mapping(path).row_count == 4
    assert mapping_server["heads"] == ['"test-v1"']
    assert not mapping_server["ranges"]


def test_mapping_server_changed_head_triggers_validated_download(tmp_path, mapping_server):
    path = tmp_path / "actor-mapping.xml"
    prepare_actor_mapping(path)
    body = XML.replace("新有菜".encode(), "新译名".encode())
    mapping_server.update(etag='"test-v2"', body=body)
    mapping_server["ranges"].clear()
    assert prepare_actor_mapping(path).resolve("新ありな") == "新ありな（新译名）"
    assert mapping_server["heads"] == ['"test-v1"']
    assert "bytes=128-255" in mapping_server["ranges"]
    assert path.read_bytes() == body


def test_mapping_interruption_keeps_cache_and_resumes_next_start(tmp_path, mapping_server, monkeypatch):
    path = tmp_path / "actor-mapping.xml"
    path.write_bytes(XML)
    mapping_server["fail"] = True
    monkeypatch.setattr(actors, "DOWNLOAD_ATTEMPTS", 1)
    assert prepare_actor_mapping(path).row_count == 4
    assert path.read_bytes() == XML
    partials = list(tmp_path.glob(".*.part-v2-*"))
    assert partials and all(part.stat().st_size == 64 for part in partials)
    mapping_server["fail"] = False
    assert prepare_actor_mapping(path).row_count == 4
    assert "bytes=64-127" in mapping_server["ranges"]
    assert not list(tmp_path.glob(".*-download-*"))


def test_mapping_changed_version_discards_stale_parts(tmp_path, mapping_server, monkeypatch):
    path = tmp_path / "actor-mapping.xml"
    mapping_server["fail"] = True
    monkeypatch.setattr(actors, "DOWNLOAD_ATTEMPTS", 1)
    with pytest.raises(ActorMappingError, match="无有效缓存"):
        prepare_actor_mapping(path)
    stale = list(tmp_path.glob(".*.part-v2-*"))
    assert stale
    mapping_server.update(fail=False, etag='"test-v2"', body=XML.replace("新有菜".encode(), "新译名".encode()))
    assert prepare_actor_mapping(path).resolve("新ありな") == "新ありな（新译名）"
    assert all(not part.exists() for part in stale)


def test_mapping_range_unsupported_uses_single_response(tmp_path, mapping_server):
    mapping_server["ignore_range"] = True
    path = tmp_path / "actor-mapping.xml"
    assert prepare_actor_mapping(path).row_count == 4
    assert path.read_bytes() == XML
    assert mapping_server["ranges"] == ["bytes=0-0"]


def test_mapping_invalid_ranged_xml_preserves_cache(tmp_path, mapping_server):
    path = tmp_path / "actor-mapping.xml"
    path.write_bytes(XML)
    mapping_server["body"] = b"<html/>"
    assert prepare_actor_mapping(path).row_count == 4
    assert path.read_bytes() == XML
    assert not list(tmp_path.glob(".*-download-*"))


def test_mapping_probe_network_failure_retries(tmp_path):
    session = Session()
    real_get = session.get
    attempts = []

    def flaky_get(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise requests.ReadTimeout("temporary timeout")
        return real_get(*args, **kwargs)

    session.get = flaky_get
    assert prepare_actor_mapping(tmp_path / "mapping.xml", session=session).row_count == 4
    assert len(attempts) == 3


def test_mapping_weak_etag_requires_full_download(tmp_path, mapping_server):
    mapping_server["etag"] = 'W/"test-v1"'
    path = tmp_path / "actor-mapping.xml"
    assert prepare_actor_mapping(path).row_count == 4
    assert mapping_server["ranges"] == ["bytes=0-0", None]


def test_mapping_version_change_during_download_reprobes(tmp_path, mapping_server):
    mapping_server["change_during_download"] = True
    path = tmp_path / "actor-mapping.xml"
    assert prepare_actor_mapping(path).row_count == 4
    assert path.read_bytes() == XML
    assert json.loads(path.with_suffix(".json").read_text())["etag"] == '"test-v2"'
    assert not list(tmp_path.glob(".*-download-*"))
