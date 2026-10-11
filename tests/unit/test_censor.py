"""离线验证打码状态在目录命名和 NFO 分类中的映射。"""

from pathlib import Path

import pytest
import yaml
from confz import FileSource
from lxml import etree

from javsp.config import Cfg
from javsp.datatype import Movie, MovieInfo
from javsp.publish.nfo import write_nfo
from javsp.publish.processor import generate_names


@pytest.mark.parametrize("representations", [None, ["未打码", "已打码", "待确认"]])
@pytest.mark.parametrize(("uncensored", "index"), [(True, 0), (False, 1), (None, 2)])
def test_censor_in_names_and_nfo(tmp_path, representations, uncensored, index):
    defaults = Path(__file__).resolve().parents[2] / "config_default.yml"
    data = yaml.safe_load(defaults.read_text(encoding="utf-8"))
    summarizer = data["summarizer"]
    if representations is not None:
        summarizer["censor_options_representation"] = representations
    expected = summarizer["censor_options_representation"][index]
    summarizer["path"]["output_folder_pattern"] = str(tmp_path / "{censor}" / "{num}")
    summarizer["path"]["basename_pattern"] = "{num}-{censor}"
    summarizer["nfo"]["title_pattern"] = "{num} {censor}"
    config_file = tmp_path / "censor-config.yml"
    config_file.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    cfg = Cfg(config_sources=FileSource(file=str(config_file)))
    info = MovieInfo("ABC-123", title="测试", uncensored=uncensored)
    assert info.get_info_dic(cfg)["censor"] == expected

    movie = Movie("ABC-123")
    movie.files = [str(tmp_path / "ABC-123.mp4")]
    movie.info = info
    generate_names(movie, cfg)

    assert movie.save_dir == str(tmp_path / expected / "ABC-123")
    assert movie.basename == f"ABC-123-{expected}"
    assert info.nfo_title == f"ABC-123 {expected}"

    nfo_file = tmp_path / "movie.nfo"
    write_nfo(info, nfo_file, cfg)
    xml = etree.parse(str(nfo_file))
    assert xml.xpath("/movie/title/text()") == [f"ABC-123 {expected}"]
    assert xml.xpath("/movie/genre/text()") == [expected]
    assert xml.xpath("/movie/tag/text()") == [expected]
