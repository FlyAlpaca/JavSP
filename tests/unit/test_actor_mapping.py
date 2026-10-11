"""演员名称格式、别名开关与 GitHub 缓存更新。"""

import pytest
import requests
from lxml import etree

from javsp import startup
from javsp.config import load_config
from javsp.datatype import Movie, MovieInfo
from javsp.metadata import actors, summarizer
from javsp.metadata.actors import ActorMapping, ActorMappingError, prepare_actor_mapping
from javsp.metadata.summarizer import MetadataMerger
from javsp.publish.nfo import write_nfo
from javsp.publish.processor import generate_names
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
    def __init__(self, response=None, error=None):
        self.response = response or Response()
        self.error = error
        self.calls = []

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
    assert unchanged.calls[0][1]["headers"]["If-None-Match"] == '"v1"'
    assert path.stat().st_mtime_ns == stamp
    new_body = XML.replace("新有菜".encode(), "新译名".encode())
    updated = Session(Response(new_body, etag='"v2"'))
    assert prepare_actor_mapping(path, session=updated).resolve("新ありな") == "新ありな（新译名）"
    assert path.read_bytes() == new_body
    assert not list(path.parent.glob(".actor-mapping-*"))


@pytest.mark.parametrize(
    "response", [Response(b"<html/>"), Response(status=503), Response(error=requests.ConnectionError("interrupted"))]
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
    path.with_suffix(".json").write_text('{"etag":"old","sha256":"mismatch"}')
    refetch = Session()
    prepare_actor_mapping(path, session=refetch)
    assert "If-None-Match" not in refetch.calls[0][1]["headers"]


def test_atomic_publication_failure_keeps_old_mapping(tmp_path, monkeypatch):
    path = tmp_path / "actor-mapping.xml"
    prepare_actor_mapping(path, session=Session())

    def denied(*_args):
        raise OSError("replace denied")

    monkeypatch.setattr(actors.os, "replace", denied)
    new_body = XML.replace("新有菜".encode(), "新译名".encode())
    assert prepare_actor_mapping(path, session=Session(Response(new_body))).resolve("新ありな") == "新ありな（新有菜）"
    assert path.read_bytes() == XML
    assert not list(tmp_path.glob(".actor-mapping-*"))


def test_size_and_download_time_limits_preserve_cache(tmp_path, monkeypatch):
    path = tmp_path / "actor-mapping.xml"
    prepare_actor_mapping(path, session=Session())
    large = Response()
    large.headers["Content-Length"] = str(actors.MAX_MAPPING_SIZE + 1)
    prepare_actor_mapping(path, session=Session(large))
    clock = iter([0, actors.DOWNLOAD_DEADLINE + 1])
    monkeypatch.setattr(actors.time, "monotonic", lambda: next(clock))
    prepare_actor_mapping(path, session=Session())
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
