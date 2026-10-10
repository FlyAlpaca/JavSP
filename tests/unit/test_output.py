"""单片产物准备、提交和恢复，全部使用本地图片和临时媒体。"""

import errno
import json
from pathlib import Path

import pytest
from PIL import Image

from javsp import output
from javsp.config import Cfg
from javsp.datatype import Movie, MovieInfo
from javsp.output import OutputExecutor, OutputPlanner, RecoveryError


def config_for(tmp_path, move=True, **summary):
    cfg = Cfg()
    paths = cfg.summarizer.path.model_copy(update={"output_folder_pattern": str(tmp_path / "out"), "hard_link": False})
    extra = cfg.summarizer.extra_fanarts.model_copy(update={"enabled": False})
    summarizer = cfg.summarizer.model_copy(update={"path": paths, "extra_fanarts": extra, "move_files": move, **summary})
    translator = cfg.translator.model_copy(update={"engine": None})
    scanner = cfg.scanner.model_copy(update={"minimum_size": 0, "skip_nfo_dir": False})
    return cfg.model_copy(update={"summarizer": summarizer, "translator": translator, "scanner": scanner})


@pytest.fixture
def scene(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    video = source / "ABC-123.mp4"
    video.write_bytes(b"video content")
    (source / "ABC-123.srt").write_bytes(b"subtitle content")
    cover = tmp_path / "cover.jpg"
    Image.new("RGB", (800, 600), "blue").save(cover)
    movie = Movie("ABC-123")
    movie.files = [str(video)]
    info = MovieInfo("ABC-123", title="title", cover=str(cover), covers=[str(cover)], uncensored=False)
    info.field_sources = {"title": "local", "cover": "local"}
    return movie, info, config_for(tmp_path)


def files_under(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_planning_is_read_only_and_does_not_mutate_input(scene, tmp_path):
    movie, info, cfg = scene
    before = files_under(tmp_path)
    plan = OutputPlanner(cfg).plan(movie, info)
    assert files_under(tmp_path) == before
    assert movie.info is None and movie.save_dir is None and info.label is None
    assert plan.movie.nfo_file.endswith("movie.nfo")
    assert len(plan.operations) == 2


def test_success_commits_all_artifacts(scene, tmp_path):
    movie, info, cfg = scene
    result = OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    target = tmp_path / "out"
    assert (target / "ABC-123.mp4").read_bytes() == b"video content"
    assert (target / "ABC-123.srt").read_bytes() == b"subtitle content"
    assert {Path(p).name for p in result.artifacts} == {"fanart.jpg", "poster.jpg", "folder.jpg", "movie.nfo"}
    assert not (tmp_path / "src").exists()
    assert not list(tmp_path.rglob(".javsp-*"))
    assert result.movie.files == [str(target / "ABC-123.mp4")]


@pytest.mark.parametrize("phase", ["prepare", "publish", "cancel"])
def test_failure_restores_original_files_and_existing_outputs(scene, tmp_path, monkeypatch, phase):
    movie, info, cfg = scene
    target = tmp_path / "out"
    target.mkdir()
    for name in ("fanart.jpg", "poster.jpg", "folder.jpg", "movie.nfo"):
        (target / name).write_bytes(b"original " + name.encode())
    before = files_under(tmp_path)
    if phase == "prepare":
        def fail(*args):
            raise OSError("nfo write failed")
        monkeypatch.setattr(output, "write_nfo", fail)
    else:
        original = output._Transaction.publish
        def publish(tx, staged, dest, stamp):
            original(tx, staged, dest, stamp)
            if dest.endswith("poster.jpg"):
                if phase == "cancel":
                    raise KeyboardInterrupt()
                raise OSError("publish failed")
        monkeypatch.setattr(output._Transaction, "publish", publish)
    error = KeyboardInterrupt if phase == "cancel" else OSError
    with pytest.raises(error):
        OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    assert files_under(tmp_path) == before
    assert not list(tmp_path.rglob(".javsp-*"))


def test_prepare_failure_removes_created_workspaces(scene, tmp_path, monkeypatch):
    movie, info, cfg = scene
    monkeypatch.setattr(output, "download_cover", lambda *a, **kw: (None, "no cover"))
    with pytest.raises(ValueError, match="下载封面"):
        OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.rglob(".javsp-*"))
    assert Path(movie.files[0]).exists()


def test_actual_format_and_folder_alias(scene, tmp_path):
    movie, info, cfg = scene
    Image.new("RGB", (800, 600), "red").save(info.cover, format="PNG")
    cover_cfg = cfg.summarizer.cover.model_copy(update={"basename_pattern": "folder"})
    cfg = cfg.model_copy(update={"summarizer": cfg.summarizer.model_copy(update={"cover": cover_cfg})})
    result = OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    assert result.movie.poster_file.endswith("folder.png")
    assert result.movie.fanart_file.endswith("fanart.png")
    with Image.open(tmp_path / "out" / "folder.jpg") as image:
        assert image.width <= 300 and image.height <= 450


def test_jpeg_folder_alias_is_full_size(scene, tmp_path):
    movie, info, cfg = scene
    cover = cfg.summarizer.cover.model_copy(update={"basename_pattern": "folder"})
    cfg = cfg.model_copy(update={"summarizer": cfg.summarizer.model_copy(update={"cover": cover})})
    result = OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    assert len(result.artifacts) == 3
    with Image.open(tmp_path / "out" / "folder.jpg") as image:
        assert image.height == 600


def test_no_move_and_shared_directory_updates(scene, tmp_path):
    movie, info, _ = scene
    cfg = config_for(tmp_path, move=False)
    first = OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    other = Movie("ABC-124")
    video = tmp_path / "src" / "ABC-124.mp4"
    video.write_bytes(b"second video")
    other.files = [str(video)]
    info = info.model_copy(update={"dvdid": "ABC-124", "title": "second title"})
    result = OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(other, info))
    assert not first.moved_files and not result.moved_files
    assert Path(movie.files[0]).exists() and video.exists()
    assert "second title" in (video.parent / "movie.nfo").read_text()
    assert not list(tmp_path.rglob(".javsp-*"))


def test_cross_device_move_and_hardlink_copy_fallback(scene, tmp_path, monkeypatch):
    movie, info, cfg = scene
    rename = output.os.rename
    def cross_device(source, dest):
        if str(source).startswith(str(tmp_path / "src")):
            raise OSError(errno.EXDEV, "cross device")
        return rename(source, dest)
    monkeypatch.setattr(output.os, "rename", cross_device)
    result = OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    assert Path(result.movie.files[0]).read_bytes() == b"video content"
    assert not Path(movie.files[0]).exists()


def test_changed_source_is_rejected_before_commit(scene, tmp_path, monkeypatch):
    movie, info, cfg = scene
    executor = OutputExecutor(cfg)
    prepare = executor._prepare
    def changed(plan, tx):
        result = prepare(plan, tx)
        Path(movie.files[0]).write_bytes(b"changed by another process")
        return result
    monkeypatch.setattr(executor, "_prepare", changed)
    with pytest.raises(OSError, match="被修改"):
        executor.execute(OutputPlanner(cfg).plan(movie, info))
    assert Path(movie.files[0]).read_bytes() == b"changed by another process"
    assert not (tmp_path / "out").exists()


def test_rollback_does_not_overwrite_external_change(scene, tmp_path, monkeypatch):
    movie, info, cfg = scene
    original = output._Transaction.publish
    def publish(tx, staged, target, stamp):
        original(tx, staged, target, stamp)
        if target.endswith("poster.jpg"):
            Path(target).write_bytes(b"external change")
            raise OSError("publish failed")
    monkeypatch.setattr(output._Transaction, "publish", publish)
    with pytest.raises(RecoveryError) as error:
        OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    assert (tmp_path / "out" / "poster.jpg").read_bytes() == b"external change"
    assert Path(movie.files[0]).read_bytes() == b"video content"
    manifests = [Path(path) / "recovery.json" for path in error.value.paths]
    assert manifests and all(path.exists() for path in manifests)
    assert json.loads(manifests[0].read_text())["errors"]


def test_overlapping_output_is_rejected(scene):
    movie, info, cfg = scene
    cover = cfg.summarizer.cover.model_copy(update={"basename_pattern": "fanart"})
    cfg = cfg.model_copy(update={"summarizer": cfg.summarizer.model_copy(update={"cover": cover})})
    with pytest.raises(ValueError, match="重叠"):
        OutputPlanner(cfg).plan(movie, info)


def test_cross_device_rollback_restores_source(scene, tmp_path, monkeypatch):
    movie, info, cfg = scene
    rename = output.os.rename
    publish = output._Transaction.publish
    def cross_device(source, dest):
        if str(source) in movie.files or (str(dest) in movie.files and Path(source).parent == tmp_path / "out"):
            raise OSError(errno.EXDEV, "cross device")
        return rename(source, dest)
    def fail(tx, staged, target, stamp):
        publish(tx, staged, target, stamp)
        raise OSError("metadata failed")
    monkeypatch.setattr(output.os, "rename", cross_device)
    monkeypatch.setattr(output._Transaction, "publish", fail)
    before = files_under(tmp_path)
    with pytest.raises(OSError, match="metadata failed"):
        OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    assert files_under(tmp_path) == before
    assert not list(tmp_path.rglob(".javsp-*"))


@pytest.mark.parametrize("fallback", [False, True])
def test_hardlink_preserves_sources(scene, monkeypatch, fallback):
    movie, info, cfg = scene
    paths = cfg.summarizer.path.model_copy(update={"hard_link": True})
    cfg = cfg.model_copy(update={"summarizer": cfg.summarizer.model_copy(update={"path": paths})})
    if fallback:
        def fail(*args):
            raise OSError("unsupported")
        monkeypatch.setattr(output.os, "link", fail)
    result = OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    source, target = Path(movie.files[0]), Path(result.movie.files[0])
    assert source.read_bytes() == target.read_bytes() == b"video content"
    assert source.samefile(target) is not fallback


def test_preserves_original_backup_when_rollback_target_changed(scene, tmp_path, monkeypatch):
    movie, info, cfg = scene
    target = tmp_path / "out"
    target.mkdir()
    (target / "poster.jpg").write_bytes(b"original poster")
    publish = output._Transaction.publish
    def fail(tx, staged, path, stamp):
        publish(tx, staged, path, stamp)
        if path.endswith("poster.jpg"):
            Path(path).write_bytes(b"external poster")
            raise OSError("failed")
    monkeypatch.setattr(output._Transaction, "publish", fail)
    with pytest.raises(RecoveryError) as error:
        OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    assert (target / "poster.jpg").read_bytes() == b"external poster"
    manifest = json.loads((Path(error.value.paths[0]) / "recovery.json").read_text())
    backup = next(e["backup"] for e in manifest["entries"] if e["target"].endswith("poster.jpg"))
    assert Path(backup).read_bytes() == b"original poster"
