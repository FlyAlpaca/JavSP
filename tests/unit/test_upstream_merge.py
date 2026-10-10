"""验证上游合并后的配置兼容性、分类过滤和目录异常处理。"""

import os
from types import SimpleNamespace

from lxml import etree

from javsp import config, file, nfo
from javsp.datatype import GenreMap, MovieInfo


def test_config_subdirectory_and_root_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root_config = tmp_path / "config.yml"
    root_config.write_text("scanner: {}", encoding="utf-8")
    assert config._find_user_config() == str(root_config)
    nested_config = tmp_path / "config" / "config.yml"
    nested_config.parent.mkdir()
    nested_config.write_text("scanner: {}", encoding="utf-8")
    assert config._find_user_config() == str(nested_config)


def test_partial_user_config_keeps_defaults(tmp_path, monkeypatch):
    user_config = tmp_path / "config.yml"
    user_config.write_text("scanner:\n  manual: true\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "_config_sources", None)
    sources = config.get_config_source()
    with config.Cfg.change_config_sources(sources):
        cfg = config.Cfg()
        assert cfg.scanner.manual is True
        assert cfg.scanner.minimum_size == 232 * 2**20


def test_genre_translation_removal_and_order(tmp_path):
    csv_file = tmp_path / "genres.csv"
    csv_file.write_text(
        "id,zh_tw,ja,zh_cn,translate\n"
        "1,繁體,日本語,简体,旧译文\n"
        "2,删除,削除,,\n",
        encoding="utf-8-sig",
    )
    mapping = GenreMap(str(csv_file))
    assert mapping.map(["1", "繁體", "日本語", "2", "删除", "未知"]) == ["简体", "未知"]


def test_scan_unreadable_child_continues(tmp_path, monkeypatch):
    inaccessible = tmp_path / "inaccessible"
    inaccessible.mkdir()
    movie_file = tmp_path / "ABC-123.mp4"
    with movie_file.open("wb") as stream:
        stream.truncate(512 * 2**20)
    original_listdir = os.listdir

    def listdir(path):
        if os.fspath(path) == str(inaccessible):
            raise PermissionError("denied")
        return original_listdir(path)

    monkeypatch.setattr(file.os, "listdir", listdir)
    assert [movie.dvdid for movie in file.scan_movies(str(tmp_path))] == ["ABC-123"]


def test_nfo_site_categories_are_tags(tmp_path, monkeypatch):
    cfg = SimpleNamespace(summarizer=SimpleNamespace(nfo=SimpleNamespace(
        custom_genres_fields=["自定义"], custom_tags_fields=["附加"],
    )))
    monkeypatch.setattr(nfo, "Cfg", lambda: cfg)
    info = MovieInfo("ABC-123", title="测试", genre=["原始"], genre_norm=["归一化", "归一化"])
    target = tmp_path / "movie.nfo"
    nfo.write_nfo(info, target)
    xml = etree.parse(str(target))
    assert xml.xpath("/movie/genre/text()") == ["自定义"]
    assert xml.xpath("/movie/tag/text()") == ["归一化", "附加"]
