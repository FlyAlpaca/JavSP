"""离线验证不同来源的评分可用于整理命名和 NFO 输出。"""

import json
from pathlib import Path

import pytest
import yaml
from confz import FileSource
from lxml import etree

from javsp.config import Cfg
from javsp.datatype import Movie, MovieInfo
from javsp.publish.nfo import write_nfo
from javsp.publish.processor import generate_names


@pytest.fixture
def score_config(tmp_path):
    defaults = Path(__file__).resolve().parents[2] / "config_default.yml"
    data = yaml.safe_load(defaults.read_text(encoding="utf-8"))
    data["summarizer"]["path"]["output_folder_pattern"] = str(tmp_path / "{score}")
    data["summarizer"]["path"]["basename_pattern"] = "{num}-{score}"
    data["summarizer"]["nfo"]["title_pattern"] = "{num} {title} {score}"
    data["summarizer"]["nfo"]["custom_tags_fields"] = ["评分：{score}"]
    config_file = tmp_path / "score-config.yml"
    config_file.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return Cfg(config_sources=FileSource(file=str(config_file)))


@pytest.mark.parametrize(
    ("score", "source", "expected"),
    [
        (8.5, "constructor", "8.5"),
        ("8.50", "constructor", "8.5"),
        (8.5, "json", "8.5"),
        ("8.50", "crawler", "8.50"),
        (0.0, "constructor", "0"),
        (None, "constructor", "0"),
    ],
)
def test_score_in_names_and_nfo(tmp_path, score_config, score, source, expected):
    if source == "json":
        metadata = tmp_path / "metadata.json"
        metadata.write_text(json.dumps({"dvdid": "ABC-123", "title": "测试", "score": score}), encoding="utf-8")
        info = MovieInfo(from_file=str(metadata))
    elif source == "crawler":
        info = MovieInfo("ABC-123", title="测试")
        info.score = score
    else:
        info = MovieInfo("ABC-123", title="测试", score=score)

    movie = Movie("ABC-123")
    movie.files = [str(tmp_path / "ABC-123.mp4")]
    movie.info = info
    generate_names(movie, cfg=score_config)

    assert movie.save_dir == str(tmp_path / expected)
    assert movie.basename == f"ABC-123-{expected}"
    assert info.nfo_title == f"ABC-123 测试 {expected}"

    nfo_file = tmp_path / "movie.nfo"
    write_nfo(info, nfo_file, cfg=score_config)
    xml = etree.parse(str(nfo_file))
    assert xml.xpath("/movie/tag/text()") == [f"评分：{expected}"]
    assert xml.xpath("/movie/rating/text()") == ([str(info.score)] if info.score else [])
    if source != "crawler":
        assert info.score == (float(score) if score is not None else None)
