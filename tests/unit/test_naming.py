"""输出名称的字节限制、完整路径预算和规划失败边界。"""

from pathlib import Path

import pytest
from confz import FileSource

from javsp.config import Cfg
from javsp.datatype import Movie, MovieInfo
from javsp.publish.naming import generate_names, validate_output_paths
from javsp.publish.planner import OutputPlanner


@pytest.fixture
def cfg():
    return Cfg(config_sources=FileSource(file='config_default.yml'))


def configure(cfg, **paths):
    path = cfg.summarizer.path.model_copy(update=paths)
    return cfg.model_copy(update={'summarizer': cfg.summarizer.model_copy(update={'path': path})})


def movie_at(tmp_path, **metadata):
    source = tmp_path / 'source'
    source.mkdir()
    video = source / 'MIZD-304.mp4'
    video.write_bytes(b'video')
    movie = Movie('MIZD-304')
    movie.files = [str(video)]
    movie.info = MovieInfo(movie, **metadata)
    return movie


@pytest.mark.parametrize('length_by_byte', [True, False])
def test_long_chinese_directory_respects_component_limit(tmp_path, cfg, length_by_byte):
    cfg = configure(cfg, output_folder_pattern='out/[{num}] {title}', length_maximum=4095, length_by_byte=length_by_byte)
    title = '很长的中文标题' * 50
    movie = movie_at(tmp_path, title=title)
    generate_names(movie, cfg, base_dir=tmp_path)
    assert max(len(part.encode('utf-8')) for part in Path(movie.save_dir).parts) <= 255
    assert movie.info.title == title
    assert movie.info.nfo_title == 'MIZD-304 ' + title
    assert movie.basename == 'MIZD-304'
    Path(movie.save_dir).mkdir(parents=True)


def test_long_actor_directory_preserves_metadata_and_title(tmp_path, cfg):
    actors = [f'演员{i}（' + '很长的中文译名' * 2 + '）' for i in range(10)]
    movie = movie_at(tmp_path, title='完整标题', actress=actors)
    cfg = configure(cfg, output_folder_pattern='out/{actress}/[{num}] {title}')
    original = movie.info.model_dump()
    plan = OutputPlanner(cfg, root=tmp_path).plan(movie, movie.info)
    assert movie.info.model_dump() == original
    assert plan.movie.info.actress == actors
    assert '…' in plan.movie.save_dir
    assert actors[0] in plan.movie.save_dir
    for part in Path(plan.movie.save_dir).parts:
        assert len(part.encode('utf-8')) <= 255
    assert len(plan.movie.nfo_file.encode('utf-8')) <= cfg.summarizer.path.length_maximum
    Path(plan.movie.save_dir).mkdir(parents=True)


@pytest.mark.parametrize('kind', ['nfo', 'cover', 'fanart'])
def test_artifact_templates_are_included_in_budget(tmp_path, cfg, kind):
    title = '中文标题' * 20
    movie = movie_at(tmp_path, title=title)
    cfg = configure(cfg, output_folder_pattern='out/{num}', length_maximum=4095)
    section = getattr(cfg.summarizer, kind).model_copy(update={'basename_pattern': '{title}' * 4})
    cfg = cfg.model_copy(update={'summarizer': cfg.summarizer.model_copy(update={kind: section})})
    plan = OutputPlanner(cfg, root=tmp_path).plan(movie, movie.info)
    assert plan.movie.info.title == title
    for path in plan.artifacts:
        assert len(Path(path).name.encode('utf-8')) <= 255
    assert movie.files == [str(tmp_path / 'source/MIZD-304.mp4')]


def test_cd_suffixes_and_image_extension_are_reserved(tmp_path, cfg):
    movie = movie_at(tmp_path, title='标题' * 80)
    second = tmp_path / 'source/MIZD-304-CD2.mkv'
    second.write_bytes(b'video2')
    movie.files.append(str(second))
    cfg = configure(cfg, output_folder_pattern='out/{num}', basename_pattern='{title}', length_maximum=4095)
    cover = cfg.summarizer.cover.model_copy(update={'basename_pattern': '{title}'})
    cfg = cfg.model_copy(update={'summarizer': cfg.summarizer.model_copy(update={'cover': cover})})
    plan = OutputPlanner(cfg, root=tmp_path).plan(movie, movie.info)
    assert len(plan.operations) == 2
    assert plan.operations[1].target.endswith('-CD2.mkv')
    for op in plan.operations:
        assert len(Path(op.target).name.encode('utf-8')) <= 255
    webp = str(Path(plan.movie.poster_file).with_suffix('.webp'))
    validate_output_paths([webp], cfg)


def test_unshortenable_literal_fails_during_read_only_planning(tmp_path, cfg):
    movie = movie_at(tmp_path, title='标题')
    cfg = configure(cfg, output_folder_pattern='out/' + '长' * 100, length_maximum=4095)
    before = movie.info.model_dump()
    with pytest.raises(ValueError, match='单个目录或文件名超过文件系统限制'):
        OutputPlanner(cfg, root=tmp_path).plan(movie, movie.info)
    assert movie.info.model_dump() == before
    assert Path(movie.files[0]).read_bytes() == b'video'
    assert not (tmp_path / 'out').exists()
    assert not list(tmp_path.rglob('.javsp-*'))


def test_impossible_path_budget_fails_without_touching_source(tmp_path, cfg):
    movie = movie_at(tmp_path, title='标题')
    cfg = configure(cfg, output_folder_pattern='out/{num}', length_maximum=10)
    with pytest.raises(ValueError, match='完整路径超过配置长度限制'):
        OutputPlanner(cfg, root=tmp_path).plan(movie, movie.info)
    assert Path(movie.files[0]).read_bytes() == b'video'
    assert not (tmp_path / 'out').exists()


def test_no_move_only_shortens_artifact_names(tmp_path, cfg):
    movie = movie_at(tmp_path, title='中文标题' * 50)
    cfg = cfg.model_copy(update={'summarizer': cfg.summarizer.model_copy(update={'move_files': False})})
    nfo = cfg.summarizer.nfo.model_copy(update={'basename_pattern': '{title}' * 3})
    cfg = cfg.model_copy(update={'summarizer': cfg.summarizer.model_copy(update={'nfo': nfo})})
    source = movie.files[0]
    plan = OutputPlanner(cfg, root=tmp_path).plan(movie, movie.info)
    assert not plan.operations
    assert plan.movie.basename == 'MIZD-304'
    assert plan.movie.save_dir == str(Path(source).parent)
    assert Path(source).read_bytes() == b'video'
    assert len(plan.movie.nfo_file.encode('utf-8')) <= cfg.summarizer.path.length_maximum


def test_filesystem_name_limit_is_honored(tmp_path, cfg, monkeypatch):
    import os

    monkeypatch.setattr(os, 'pathconf', lambda path, name: 64 if name == 'PC_NAME_MAX' else 4096)
    movie = movie_at(tmp_path, title='标题' * 30)
    cfg = configure(cfg, output_folder_pattern='out/{num}/{title}', length_maximum=4095)
    plan = OutputPlanner(cfg, root=tmp_path).plan(movie, movie.info)
    assert len(Path(plan.movie.save_dir).name.encode('utf-8')) <= 64
