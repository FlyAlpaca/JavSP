from javsp.cli import summarize_batch
from javsp.publish import transaction

"""离线贯通扫描、抓取、合并、规划和输出，验证批次停止边界。"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import etree
from PIL import Image

from javsp.config import load_config
from javsp.datatype import Movie
from javsp.web.exceptions import MovieNotFoundError
from javsp.workflow.models import Stage
from javsp.workflow.pipeline import JobRunner, Pipeline


def run_in_id_order(pipeline, source):
    """批次边界测试需要固定处理顺序，扫描本身不保证文件顺序。"""
    return pipeline.run(source, review=lambda movies, root: movies.sort(key=lambda movie: movie.dvdid))


@pytest.fixture
def scene(tmp_path, monkeypatch):
    cfg = load_config()
    cfg = cfg.model_copy(
        update={
            "scanner": cfg.scanner.model_copy(update={"minimum_size": 0, "skip_nfo_dir": False}),
            "crawler": cfg.crawler.model_copy(
                update={"selection": {"normal": ["testlocal"]}, "normalize_actress_name": False, "online_enabled": True}
            ),
            "r18dev": cfg.r18dev.model_copy(update={"enabled": False}),
            "summarizer": cfg.summarizer.model_copy(
                update={
                    "path": cfg.summarizer.path.model_copy(update={"output_folder_pattern": str(tmp_path / "out" / "{num}")}),
                    "extra_fanarts": cfg.summarizer.extra_fanarts.model_copy(update={"enabled": False}),
                }
            ),
        }
    )
    source = tmp_path / "src"
    source.mkdir()
    for identity in ("ABC-123", "ABC-124"):
        (source / f"{identity}.mp4").write_bytes(identity.encode())
    cover = tmp_path / "cover.jpg"
    Image.new("RGB", (800, 600), "blue").save(cover)

    def parse(info, *, context):
        info.title, info.cover = "local title", str(cover)
        info.uncensored = False

    monkeypatch.setitem(sys.modules, "javsp.web.testlocal", SimpleNamespace(parse_data=parse))
    throttle = []
    monkeypatch.setattr("javsp.crawl.dispatcher.CrawlerExecutor.throttle", lambda self: throttle.append(True))
    return cfg, source, throttle


def test_offline_pipeline_end_to_end(scene, tmp_path):
    cfg, source, throttle = scene
    events = []
    result = run_in_id_order(Pipeline(cfg, events.append, translator=SimpleNamespace(translate=lambda text, field: text)), source)
    assert result.exit_code == 0 and len(result.jobs) == 2
    assert result.scan.movies == [job.movie for job in result.jobs]
    for job in result.jobs:
        target = tmp_path / "out" / job.movie.dvdid
        assert (target / "movie.nfo").exists()
        assert Path(job.movie.files[0]).read_bytes() == job.movie.dvdid.encode()
        assert job.movie.info.field_sources["title"] == "testlocal"
    assert throttle == [True]
    assert [e.stage for e in events] == [Stage.SCAN] + [
        Stage.CRAWL,
        Stage.CRAWL,
        Stage.MERGE,
        Stage.TRANSLATE,
        Stage.PLAN,
        Stage.PREPARE,
        Stage.COMMIT,
        Stage.DONE,
    ] * 2
    successful, stats = summarize_batch(result, True)
    assert len(successful) == stats["success"] == 2 and stats["failed"] == 0
    assert len(stats["filemove_list"]) == 2
    assert source.is_dir()


def test_local_translation_updates_nfo_and_directory_name(scene, tmp_path, monkeypatch):
    cfg, source, _ = scene
    path_cfg = cfg.summarizer.path.model_copy(update={"output_folder_pattern": str(tmp_path / "out" / "{num} {title}")})
    cfg = cfg.model_copy(update={"summarizer": cfg.summarizer.model_copy(update={"path": path_cfg})})
    original = sys.modules["javsp.web.testlocal"].parse_data

    def parse(info, *, context):
        original(info, context=context)
        info.title = "日本語タイトル"
        info.plot = "日本語の紹介。"

    class Translator:
        def translate(self, text, field):
            return {"title": "中文标题", "plot": "中文简介。"}[field]

    monkeypatch.setattr(sys.modules["javsp.web.testlocal"], "parse_data", parse)
    result = run_in_id_order(Pipeline(cfg, translator=Translator()), source)
    assert result.exit_code == 0
    target = tmp_path / "out" / "ABC-123 中文标题"
    nfo = etree.parse(str(target / "movie.nfo"))
    assert nfo.xpath("/movie/title/text()") == ["ABC-123 中文标题"]
    assert nfo.xpath("/movie/originaltitle/text()") == ["日本語タイトル"]
    assert nfo.xpath("/movie/plot/text()") == ["中文简介。"]


def test_batch_continues_after_crawl_failure(scene, monkeypatch):
    cfg, source, throttle = scene
    parser = sys.modules["javsp.web.testlocal"].parse_data

    def parse(info, *, context):
        if info.dvdid == "ABC-123":
            raise MovieNotFoundError("testlocal", info.dvdid)
        parser(info, context=context)

    monkeypatch.setattr(sys.modules["javsp.web.testlocal"], "parse_data", parse)
    result = run_in_id_order(Pipeline(cfg, translator=SimpleNamespace(translate=lambda text, field: text)), source)
    assert result.exit_code == 1 and not result.aborted
    assert result.jobs[0].error.stage == Stage.CRAWL
    assert result.jobs[1].success
    assert not throttle
    assert (source / "ABC-123.mp4").exists()


@pytest.mark.parametrize("failure", ["publish", "cancel", "recovery"])
def test_batch_recovery_and_stop_policy(scene, tmp_path, monkeypatch, failure):
    cfg, source, throttle = scene
    publish = transaction.FileTransaction.publish

    def fail(tx, staged, target, stamp):
        publish(tx, staged, target, stamp)
        if "ABC-123" in target:
            if failure == "cancel":
                raise KeyboardInterrupt()
            if failure == "recovery":
                Path(target).write_bytes(b"external content")
            raise OSError("output failed")

    monkeypatch.setattr(transaction.FileTransaction, "publish", fail)
    result = run_in_id_order(Pipeline(cfg, translator=SimpleNamespace(translate=lambda text, field: text)), source)
    assert result.exit_code == 1
    assert result.jobs[0].error.stage == Stage.COMMIT
    assert (source / "ABC-123.mp4").read_bytes() == b"ABC-123"
    if failure == "publish":
        assert not result.aborted and len(result.jobs) == 2
        assert result.jobs[1].success
    else:
        assert result.aborted and len(result.jobs) == 1 and len(result.pending) == 1
        assert (source / "ABC-124.mp4").exists()
        assert not (tmp_path / "out" / "ABC-124").exists()
    assert not throttle


def test_cancel_between_jobs_keeps_successful_commit(scene, monkeypatch):
    cfg, source, _ = scene

    def cancel(self):
        raise KeyboardInterrupt()

    monkeypatch.setattr("javsp.crawl.dispatcher.CrawlerExecutor.throttle", cancel)
    result = run_in_id_order(Pipeline(cfg, translator=SimpleNamespace(translate=lambda text, field: text)), source)
    assert result.aborted and result.exit_code == 1
    assert result.jobs[0].success and len(result.pending) == 1
    assert result.error.stage == Stage.CRAWL
    assert (source / "ABC-124.mp4").exists()


def test_runner_does_not_publish_metadata_on_failure(scene):
    cfg, source, _ = scene
    movie = Movie("ABC-123")
    movie.files = [str(source / "ABC-123.mp4")]
    cfg = cfg.model_copy(update={"crawler": cfg.crawler.model_copy(update={"required_keys": ["plot"]})})
    job = JobRunner(cfg, translator=SimpleNamespace(translate=lambda text, field: text)).run(movie)
    assert job.error.stage == Stage.MERGE
    assert movie.info is None and movie.save_dir is None


def test_cancel_after_commit_keeps_successful_result(scene, tmp_path, monkeypatch):
    cfg, source, _ = scene
    cleanup = transaction.FileTransaction.cleanup
    interrupted = False

    def cancel(tx, remove_dirs=False):
        nonlocal interrupted
        result = cleanup(tx, remove_dirs)
        if not remove_dirs and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("cancel after commit")
        return result

    monkeypatch.setattr(transaction.FileTransaction, "cleanup", cancel)
    result = run_in_id_order(Pipeline(cfg, translator=SimpleNamespace(translate=lambda text, field: text)), source)
    assert result.aborted and result.exit_code == 1
    assert result.jobs[0].success and len(result.jobs) == 1
    assert result.jobs[0].movie.files == [str(tmp_path / "out" / "ABC-123" / "ABC-123.mp4")]
    assert (tmp_path / "out" / "ABC-123" / "movie.nfo").exists()
    assert len(result.pending) == 1 and (source / "ABC-124.mp4").exists()
    assert summarize_batch(result)[1]["success"] == 1


def test_relative_output_uses_scan_root_without_chdir(scene, monkeypatch, tmp_path):
    cfg, source, _ = scene
    path = cfg.summarizer.path.model_copy(update={'output_folder_pattern': '#整理完成/{num}'})
    cfg = cfg.model_copy(update={'summarizer': cfg.summarizer.model_copy(update={'path': path})})
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    before = Path.cwd()
    translator = SimpleNamespace(translate=lambda text, field: text)
    batch = run_in_id_order(Pipeline(cfg, translator=translator), source)
    assert batch.exit_code == 0
    assert Path.cwd() == before
    for job in batch.jobs:
        target = source / '#整理完成' / job.movie.dvdid
        assert Path(job.movie.save_dir) == target
        assert (target / 'movie.nfo').exists()
    assert list(unrelated.iterdir()) == []


def test_pipeline_requires_prepared_translator(scene):
    cfg, source, _ = scene
    movie = Movie('ABC-123')
    movie.files = [str(source / 'ABC-123.mp4')]
    with pytest.raises(ValueError, match='翻译器未初始化'):
        JobRunner(cfg).run(movie)
    assert Path(movie.files[0]).exists()
    assert movie.info is None


def test_scan_progress_callback_failure_does_not_abort(scene):
    cfg, source, _ = scene

    def failed_callback(event):
        raise ValueError('display unavailable')

    batch = run_in_id_order(Pipeline(cfg, failed_callback, translator=object()), source)
    assert batch.exit_code == 0


def test_pipeline_cleans_shared_nested_source_directory_after_all_jobs(scene):
    cfg, source, _ = scene
    group = source / "group"
    disc = group / "disc"
    disc.mkdir(parents=True)
    for video in source.glob("*.mp4"):
        video.rename(disc / video.name)
    (group / "old.nfo").write_bytes(b"old")
    (disc / "unmatched.ass").write_bytes(b"ordinary subtitle")
    observed = []
    batch = Pipeline(cfg, translator=object()).run(
        source, on_result=lambda job: observed.append(group.is_dir()),
    )
    assert batch.exit_code == 0 and len(batch.jobs) == 2
    assert observed == [True, True]
    assert not group.exists() and source.is_dir() and not batch.warnings


@pytest.mark.parametrize("blocker", ["unrecognized", "small", "duplicate", "failed", "conflict_subtitle"])
def test_pipeline_keeps_source_directory_with_unprocessed_files(scene, tmp_path, monkeypatch, blocker):
    cfg, source, _ = scene
    group = source / "group"
    group.mkdir()
    (source / "ABC-123.mp4").rename(group / "ABC-123.mp4")
    (group / "notes.txt").write_bytes(b"keep")
    if blocker == "unrecognized":
        (group / "unknown-video.mp4").write_bytes(b"unrecognized")
    elif blocker == "small":
        (group / "ABC-125.mp4").write_bytes(b"x")
        cfg = cfg.model_copy(update={"scanner": cfg.scanner.model_copy(update={"minimum_size": 4})})
    elif blocker == "duplicate":
        (source / "ABC-125.mp4").write_bytes(b"one")
        (group / "ABC-125.mp4").write_bytes(b"two")
    elif blocker == "failed":
        (group / "ABC-125.mp4").write_bytes(b"failed")
        parser = sys.modules["javsp.web.testlocal"].parse_data
        def parse(info, *, context):
            if info.dvdid == "ABC-125":
                raise MovieNotFoundError("testlocal", info.dvdid)
            parser(info, context=context)
        monkeypatch.setattr(sys.modules["javsp.web.testlocal"], "parse_data", parse)
    else:
        (group / "ABC-123.srt").write_bytes(b"source subtitle")
        destination = tmp_path / "out" / "ABC-123"
        destination.mkdir(parents=True)
        (destination / "ABC-123.srt").write_bytes(b"existing subtitle")
    batch = Pipeline(cfg, translator=object()).run(source)
    assert group.is_dir() and (group / "notes.txt").read_bytes() == b"keep"
    if blocker == "unrecognized":
        assert batch.scan.unrecognized
    elif blocker == "small":
        assert str(group / "ABC-125.mp4") in batch.scan.skipped
    elif blocker == "duplicate":
        assert batch.scan.duplicates
    elif blocker == "failed":
        assert batch.exit_code == 1 and not batch.aborted
    else:
        job = next(job for job in batch.jobs if job.movie.dvdid == "ABC-123")
        assert job.success and job.output.skipped_files == [str(group / "ABC-123.srt")]
        assert (group / "ABC-123.srt").read_bytes() == b"source subtitle"


def test_pipeline_cleans_successful_sibling_despite_failure_and_retains_parent(scene, monkeypatch):
    cfg, source, _ = scene
    parent = source / "group"
    failed, completed = parent / "failed", parent / "completed"
    for directory, identity in ((failed, "ABC-123"), (completed, "ABC-124")):
        directory.mkdir(parents=True)
        (source / f"{identity}.mp4").rename(directory / f"{identity}.mp4")
        (directory / "notes.txt").write_bytes(b"notes")
    parser = sys.modules["javsp.web.testlocal"].parse_data
    def parse(info, *, context):
        if info.dvdid == "ABC-123":
            raise MovieNotFoundError("testlocal", info.dvdid)
        parser(info, context=context)
    monkeypatch.setattr(sys.modules["javsp.web.testlocal"], "parse_data", parse)
    batch = run_in_id_order(Pipeline(cfg, translator=object()), source)
    assert batch.exit_code == 1 and not batch.aborted
    assert failed.is_dir() and parent.is_dir() and not completed.exists()


def test_cancelled_pipeline_retains_completed_source_directory(scene, monkeypatch):
    cfg, source, _ = scene
    group = source / "group"
    group.mkdir()
    (source / "ABC-123.mp4").rename(group / "ABC-123.mp4")
    (group / "notes.txt").write_bytes(b"keep")
    def cancel(self):
        raise KeyboardInterrupt()
    monkeypatch.setattr("javsp.crawl.dispatcher.CrawlerExecutor.throttle", cancel)
    batch = run_in_id_order(Pipeline(cfg, translator=object()), source)
    assert batch.aborted and batch.jobs[0].success
    assert group.is_dir() and (group / "notes.txt").read_bytes() == b"keep"
