from javsp.publish import transaction

"""单片产物准备、提交和恢复，全部使用本地图片和临时媒体。"""

import errno
import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic_extra_types.pendulum_dt import Duration

from javsp.config import load_config
from javsp.datatype import Movie, MovieInfo
from javsp.publish import media, output
from javsp.publish.output import OutputExecutor
from javsp.publish.planner import OutputPlanner
from javsp.publish.transaction import RecoveryError


def config_for(tmp_path, move=True, **summary):
    cfg = load_config()
    paths = cfg.summarizer.path.model_copy(update={"output_folder_pattern": str(tmp_path / "out"), "hard_link": False})
    extra = cfg.summarizer.extra_fanarts.model_copy(update={"enabled": False})
    summarizer = cfg.summarizer.model_copy(update={"path": paths, "extra_fanarts": extra, "move_files": move, **summary})
    scanner = cfg.scanner.model_copy(update={"minimum_size": 0, "skip_nfo_dir": False})
    return cfg.model_copy(update={"summarizer": summarizer, "scanner": scanner})


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


def test_long_names_commit_and_preserve_full_nfo_metadata(scene, tmp_path):
    from lxml import etree

    movie, info, cfg = scene
    title = "完整的中文标题🌟" * 50
    actors = [f"演员{i}（" + "很长的中文译名" * 2 + "）" for i in range(10)]
    info.title, info.actress = title, actors
    paths = cfg.summarizer.path.model_copy(update={"output_folder_pattern": str(tmp_path / "out/{actress}/[{num}] {title}")})
    cfg = cfg.model_copy(update={"summarizer": cfg.summarizer.model_copy(update={"path": paths})})
    plan = OutputPlanner(cfg).plan(movie, info)
    result = OutputExecutor(cfg).execute(plan)

    assert Path(result.movie.files[0]).read_bytes() == b"video content"
    assert Path(result.movie.save_dir, "ABC-123.srt").read_bytes() == b"subtitle content"
    assert not Path(movie.files[0]).exists()
    for path in result.movie.files + result.artifacts:
        assert len(path.encode("utf-8")) <= cfg.summarizer.path.length_maximum
        assert max(len(part.encode("utf-8")) for part in Path(path).parts) <= 255
    tree = etree.parse(result.movie.nfo_file)
    assert tree.xpath("/movie/title/text()") == ["ABC-123 " + title]
    assert tree.xpath("/movie/actor/name/text()") == actors
    assert info.title == title and info.actress == actors
    assert not list(tmp_path.rglob(".javsp-*"))


def test_long_literal_rejected_before_preparing_artifacts(scene, tmp_path, monkeypatch):
    movie, info, cfg = scene
    paths = cfg.summarizer.path.model_copy(update={
        "output_folder_pattern": str(tmp_path / "out" / ("长" * 100)), "length_maximum": 4095,
    })
    cfg = cfg.model_copy(update={"summarizer": cfg.summarizer.model_copy(update={"path": paths})})
    before = files_under(tmp_path)
    monkeypatch.setattr(output, "download_cover", lambda *args, **kwargs: pytest.fail("规划失败不能下载或准备产物"))
    with pytest.raises(ValueError, match="单个目录或文件名超过文件系统限制"):
        OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    assert files_under(tmp_path) == before
    assert not list(tmp_path.rglob(".javsp-*"))


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
        original = transaction.FileTransaction.publish
        def publish(tx, staged, dest, stamp):
            original(tx, staged, dest, stamp)
            if dest.endswith("poster.jpg"):
                if phase == "cancel":
                    raise KeyboardInterrupt()
                raise OSError("publish failed")
        monkeypatch.setattr(transaction.FileTransaction, "publish", publish)
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
    rename = transaction._rename_new
    def cross_device(source, dest):
        if str(source).startswith(str(tmp_path / "src")):
            raise OSError(errno.EXDEV, "cross device")
        return rename(source, dest)
    monkeypatch.setattr(transaction, "_rename_new", cross_device)
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
    original = transaction.FileTransaction.publish
    def publish(tx, staged, target, stamp):
        original(tx, staged, target, stamp)
        if target.endswith("poster.jpg"):
            Path(target).write_bytes(b"external change")
            raise OSError("publish failed")
    monkeypatch.setattr(transaction.FileTransaction, "publish", publish)
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
    rename = transaction._rename_new
    publish = transaction.FileTransaction.publish
    def cross_device(source, dest):
        if str(source) in movie.files or (str(dest) in movie.files and Path(source).parent == tmp_path / "out"):
            raise OSError(errno.EXDEV, "cross device")
        return rename(source, dest)
    def fail(tx, staged, target, stamp):
        publish(tx, staged, target, stamp)
        raise OSError("metadata failed")
    monkeypatch.setattr(transaction, "_rename_new", cross_device)
    monkeypatch.setattr(transaction.FileTransaction, "publish", fail)
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
    publish = transaction.FileTransaction.publish
    def fail(tx, staged, path, stamp):
        publish(tx, staged, path, stamp)
        if path.endswith("poster.jpg"):
            Path(path).write_bytes(b"external poster")
            raise OSError("failed")
    monkeypatch.setattr(transaction.FileTransaction, "publish", fail)
    with pytest.raises(RecoveryError) as error:
        OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    assert (target / "poster.jpg").read_bytes() == b"external poster"
    manifest = json.loads((Path(error.value.paths[0]) / "recovery.json").read_text())
    backup = next(e["backup"] for e in manifest["entries"] if e["target"].endswith("poster.jpg"))
    assert Path(backup).read_bytes() == b"original poster"


@pytest.mark.parametrize("operation", ["move", "backup", "publish"])
def test_interrupt_after_atomic_operation_still_restores(scene, tmp_path, monkeypatch, operation):
    movie, info, cfg = scene
    target = tmp_path / "out"
    target.mkdir()
    (target / "fanart.jpg").write_bytes(b"original fanart")
    before = files_under(tmp_path)
    interrupted = False
    if operation in ("move", "publish"):
        original = transaction._rename_new
        def rename(source, dest):
            nonlocal interrupted
            original(source, dest)
            should_interrupt = (str(source) == movie.files[0] if operation == "move"
                                else str(dest) == str(target / "fanart.jpg"))
            if should_interrupt and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
        monkeypatch.setattr(transaction, "_rename_new", rename)
    else:
        original = output.os.replace
        def replace(source, dest):
            nonlocal interrupted
            original(source, dest)
            should_interrupt = (str(source) == str(target / "fanart.jpg") if operation == "backup"
                                else str(dest) == str(target / "fanart.jpg"))
            if should_interrupt and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
        monkeypatch.setattr(output.os, "replace", replace)
    with pytest.raises(KeyboardInterrupt):
        OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    assert interrupted
    assert files_under(tmp_path) == before
    assert not list(tmp_path.rglob(".javsp-*"))


def test_target_created_during_video_commit_is_not_overwritten(scene, tmp_path, monkeypatch):
    movie, info, cfg = scene
    rename = transaction._rename_new
    target = tmp_path / "out" / "ABC-123.mp4"
    def race(source, dest):
        if str(source) == movie.files[0]:
            target.write_bytes(b"external video")
        return rename(source, dest)
    monkeypatch.setattr(transaction, "_rename_new", race)
    with pytest.raises(FileExistsError):
        OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    assert target.read_bytes() == b"external video"
    assert Path(movie.files[0]).read_bytes() == b"video content"
    assert not list(tmp_path.rglob(".javsp-*"))


def test_target_created_after_metadata_backup_is_preserved(scene, tmp_path, monkeypatch):
    movie, info, cfg = scene
    target = tmp_path / "out"
    target.mkdir()
    (target / "fanart.jpg").write_bytes(b"original fanart")
    rename = transaction._rename_new
    def race(source, dest):
        if str(dest) == str(target / "fanart.jpg"):
            Path(dest).write_bytes(b"external fanart")
        return rename(source, dest)
    monkeypatch.setattr(transaction, "_rename_new", race)
    with pytest.raises(RecoveryError) as error:
        OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    assert (target / "fanart.jpg").read_bytes() == b"external fanart"
    manifest = json.loads((Path(error.value.paths[0]) / "recovery.json").read_text())
    entry = next(e for e in manifest["entries"] if e["target"].endswith("fanart.jpg"))
    assert Path(entry["backup"]).read_bytes() == b"original fanart"
    assert Path(movie.files[0]).read_bytes() == b"video content"


@pytest.mark.parametrize("fail_commit", [False, True])
def test_extra_fanarts_participate_in_transaction(scene, tmp_path, monkeypatch, fail_commit):
    movie, info, cfg = scene
    extras = cfg.summarizer.extra_fanarts.model_copy(update={"enabled": True, "scrap_interval": Duration(0)})
    cfg = cfg.model_copy(update={"summarizer": cfg.summarizer.model_copy(update={"extra_fanarts": extras})})
    info.preview_pics = [info.cover, "failed-preview"]
    download = media.download
    def local_download(url, path, **kwargs):
        if url == "failed-preview":
            raise OSError("preview unavailable")
        return download(url, path, **kwargs)
    monkeypatch.setattr(media, "download", local_download)
    target = tmp_path / "out" / "extrafanart"
    target.mkdir(parents=True)
    (target / "0.jpg").write_bytes(b"original preview")
    before = files_under(tmp_path)
    if fail_commit:
        publish = transaction.FileTransaction.publish
        def fail(tx, staged, path, stamp):
            publish(tx, staged, path, stamp)
            if "extrafanart" in path:
                raise OSError("extra publish failed")
        monkeypatch.setattr(transaction.FileTransaction, "publish", fail)
        with pytest.raises(OSError, match="extra publish failed"):
            OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
        assert files_under(tmp_path) == before
    else:
        result = OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
        assert len(result.warnings) == 1 and "preview unavailable" in result.warnings[0]
        assert (target / "0.jpg").read_bytes() != b"original preview"
        assert not (target / "1.jpg").exists()
    assert not list(tmp_path.rglob(".javsp-*"))


def test_downloaded_cover_retains_actual_source(scene, tmp_path):
    movie, info, cfg = scene
    info.cover = "failed-cover"
    info.covers = [str(tmp_path / "cover.jpg")]
    info._cover_sources = {info.covers[0]: "fallback"}
    result = OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    assert result.movie.info.field_sources["cover"] == "fallback"


def test_cross_device_source_change_before_delete_is_preserved(scene, tmp_path, monkeypatch):
    movie, info, cfg = scene
    rename = transaction._rename_new
    target = tmp_path / "out" / "ABC-123.mp4"
    def race(source, dest):
        if str(source) == movie.files[0]:
            raise OSError(errno.EXDEV, "cross device")
        rename(source, dest)
        if str(dest) == str(target):
            Path(movie.files[0]).write_bytes(b"external source update")
    monkeypatch.setattr(transaction, "_rename_new", race)
    with pytest.raises(RecoveryError):
        OutputExecutor(cfg).execute(OutputPlanner(cfg).plan(movie, info))
    assert Path(movie.files[0]).read_bytes() == b"external source update"
    assert target.read_bytes() == b"video content"
