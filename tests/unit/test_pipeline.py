"""离线贯通扫描、抓取、合并、规划和输出，验证批次停止边界。"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from javsp import output
from javsp.config import Cfg
from javsp.datatype import Movie
from javsp.models import Stage
from javsp.pipeline import JobRunner, Pipeline
from javsp.web.exceptions import MovieNotFoundError


@pytest.fixture
def scene(tmp_path, monkeypatch):
    cfg = Cfg()
    cfg = cfg.model_copy(update={
        "scanner": cfg.scanner.model_copy(update={"minimum_size": 0, "skip_nfo_dir": False}),
        "translator": cfg.translator.model_copy(update={"engine": None}),
        "crawler": cfg.crawler.model_copy(update={"selection": {"normal": ["testlocal"]}, "normalize_actress_name": False}),
        "summarizer": cfg.summarizer.model_copy(update={
            "path": cfg.summarizer.path.model_copy(update={"output_folder_pattern": str(tmp_path / "out" / "{num}")}),
            "extra_fanarts": cfg.summarizer.extra_fanarts.model_copy(update={"enabled": False}),
        }),
    })
    source = tmp_path / "src"
    source.mkdir()
    for identity in ("ABC-123", "ABC-124"):
        (source / f"{identity}.mp4").write_bytes(identity.encode())
    cover = tmp_path / "cover.jpg"
    Image.new("RGB", (800, 600), "blue").save(cover)
    def parse(info):
        info.title, info.cover = "local title", str(cover)
        info.uncensored = False
    monkeypatch.setitem(sys.modules, "javsp.web.testlocal", SimpleNamespace(parse_data=parse))
    throttle = []
    monkeypatch.setattr("javsp.dispatcher.CrawlerExecutor.throttle", lambda self: throttle.append(True))
    return cfg, source, throttle


def test_offline_pipeline_end_to_end(scene, tmp_path):
    cfg, source, throttle = scene
    events = []
    result = Pipeline(cfg, events.append).run(source)
    assert result.exit_code == 0 and len(result.jobs) == 2
    assert result.scan.movies == [job.movie for job in result.jobs]
    for job in result.jobs:
        target = tmp_path / "out" / job.movie.dvdid
        assert (target / "movie.nfo").exists()
        assert Path(job.movie.files[0]).read_bytes() == job.movie.dvdid.encode()
        assert job.movie.info.field_sources["title"] == "testlocal"
    assert throttle == [True]
    assert [e.stage for e in events] == [Stage.SCAN] + [Stage.CRAWL, Stage.CRAWL, Stage.MERGE,
        Stage.PLAN, Stage.PREPARE, Stage.COMMIT, Stage.DONE] * 2
    successful, stats = result.legacy(True)
    assert len(successful) == stats["success"] == 2 and stats["failed"] == 0
    assert len(stats["filemove_list"]) == 2


def test_batch_continues_after_crawl_failure(scene, monkeypatch):
    cfg, source, throttle = scene
    parser = sys.modules["javsp.web.testlocal"].parse_data
    def parse(info):
        if info.dvdid == "ABC-123":
            raise MovieNotFoundError("not found")
        parser(info)
    monkeypatch.setattr(sys.modules["javsp.web.testlocal"], "parse_data", parse)
    result = Pipeline(cfg).run(source)
    assert result.exit_code == 1 and not result.aborted
    assert result.jobs[0].error.stage == Stage.CRAWL
    assert result.jobs[1].success
    assert not throttle
    assert (source / "ABC-123.mp4").exists()


@pytest.mark.parametrize("failure", ["publish", "cancel", "recovery"])
def test_batch_recovery_and_stop_policy(scene, tmp_path, monkeypatch, failure):
    cfg, source, throttle = scene
    publish = output._Transaction.publish
    def fail(tx, staged, target, stamp):
        publish(tx, staged, target, stamp)
        if "ABC-123" in target:
            if failure == "cancel":
                raise KeyboardInterrupt()
            if failure == "recovery":
                Path(target).write_bytes(b"external content")
            raise OSError("output failed")
    monkeypatch.setattr(output._Transaction, "publish", fail)
    result = Pipeline(cfg).run(source)
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
    monkeypatch.setattr("javsp.dispatcher.CrawlerExecutor.throttle", cancel)
    result = Pipeline(cfg).run(source)
    assert result.aborted and result.exit_code == 1
    assert result.jobs[0].success and len(result.pending) == 1
    assert result.error.stage == Stage.CRAWL
    assert (source / "ABC-124.mp4").exists()


def test_runner_does_not_publish_metadata_on_failure(scene):
    cfg, source, _ = scene
    movie = Movie("ABC-123")
    movie.files = [str(source / "ABC-123.mp4")]
    cfg = cfg.model_copy(update={"crawler": cfg.crawler.model_copy(update={"required_keys": ["plot"]})})
    job = JobRunner(cfg).run(movie)
    assert job.error.stage == Stage.MERGE
    assert movie.info is None and movie.save_dir is None
