"""验证批处理失败可被自动化识别。"""

import subprocess
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from javsp import __main__ as cli
from javsp.cli import print_summary, summarize_batch
from javsp.config import Cfg, load_config
from javsp.datatype import Movie, MovieInfo
from javsp.workflow.models import BatchResult, JobError, JobResult, OutputResult, Stage
from javsp.workflow.pipeline import JobRunner


@pytest.mark.parametrize("failed, code", [(0, 0), (1, 1), (3, 1)])
def test_entry_returns_failure_status(tmp_path, monkeypatch, failed, code):
    cfg = SimpleNamespace(
        other=SimpleNamespace(debug=False),
        scanner=SimpleNamespace(input_directory=str(tmp_path)),
        translator=object(),
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    cfg.summarizer = SimpleNamespace(filemove_log=False)
    runtime = SimpleNamespace(cfg=cfg, root=tmp_path, translator=object())
    monkeypatch.setattr(cli, "setup_filemove_log", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "print_summary", lambda *a: None)
    monkeypatch.setattr(cli, "startup_context", lambda cfg: nullcontext(runtime))
    batch = BatchResult(3)
    for index in range(3):
        movie = Movie(f"ABC-{index + 123}")
        movie.info = MovieInfo(movie)
        error = JobError(movie.dvdid, Stage.CRAWL, ValueError("failed")) if index < failed else None
        batch.jobs.append(JobResult(movie, output=None if error else OutputResult(movie), error=error))
    monkeypatch.setattr(cli, "Pipeline", lambda *a, **kw: SimpleNamespace(run=lambda *a, **kw: batch))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.entry()
    assert exc.value.code == code


def test_entry_with_missing_directory_exits_without_prompt(tmp_path, monkeypatch):
    cfg = SimpleNamespace(
        other=SimpleNamespace(debug=False),
        scanner=SimpleNamespace(input_directory=tmp_path / "missing"),
        translator=object(),
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "startup_context", lambda cfg: (_ for _ in ()).throw(cli.StartupError("扫描目录不存在")))
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("容器批处理不应提示选目录"))
    with pytest.raises(SystemExit) as exc:
        cli.entry()
    assert exc.value.code == 1


def test_model_startup_failure_precedes_media_scan(monkeypatch):
    cfg = load_config()
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    class FailedTranslator:
        def __enter__(self):
            raise RuntimeError("model unavailable")

        def __exit__(self, *_):
            pass

    monkeypatch.setattr("javsp.startup.prepare_startup", lambda _: SimpleNamespace(root="/media", translator=None))
    monkeypatch.setattr("javsp.startup.LocalTranslator", lambda _, **kw: FailedTranslator())
    monkeypatch.setattr(cli, "Pipeline", lambda *_args, **_kwargs: pytest.fail("不应扫描媒体目录"))
    with pytest.raises(RuntimeError, match="model unavailable"):
        cli.entry()


def test_invalid_config_exits_without_prompt(monkeypatch):
    with pytest.raises(ValidationError) as exc:
        Cfg.model_validate({})
    error = exc.value

    def invalid_config():
        raise error

    monkeypatch.setattr(cli, "load_config", invalid_config)
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("配置错误时不应等待输入"))
    with pytest.raises(SystemExit) as exit_result:
        cli.entry()
    assert exit_result.value.code == 1


def test_invalid_config_is_reported_after_cli_startup():
    result = subprocess.run(
        [sys.executable, "-m", "javsp", "--oscanner.minimum_size", "invalid"],
        input="",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert "配置错误 [scanner → minimum_size]" in result.stdout
    assert "Traceback" not in result.stderr


def test_batch_continues_and_counts_failure(monkeypatch):
    cfg = load_config()
    cfg = cfg.model_copy(update={"concurrency": cfg.concurrency.model_copy(update={"movies": 1})})
    movies = [Movie("ABC-123"), Movie("ABC-124")]
    visited = []

    def run(self, movie):
        visited.append(movie)
        if movie is movies[0]:
            return JobResult(movie, error=JobError("ABC-123", Stage.COMMIT, OSError("move failed")))
        movie.info = MovieInfo(movie)
        return JobResult(movie, output=OutputResult(movie))

    monkeypatch.setattr(JobRunner, "run", run)
    monkeypatch.setattr("javsp.crawl.dispatcher.CrawlerExecutor.throttle", lambda self: None)
    batch = JobRunner(cfg, translator=object()).run_batch(movies)
    successful, stats = summarize_batch(batch)
    assert visited == movies
    assert successful == [movies[1]]
    assert stats["total"] == 2 and stats["failed"] == 1 and stats["success"] == 1
    assert "move failed" in stats["failed_list"][0][1]


def failure_stats(cause, stage=Stage.CRAWL):
    movie = Movie("IPX-658")
    error = JobError(movie.dvdid, stage, cause)
    batch = BatchResult(1, jobs=[JobResult(movie, error=error)])
    return summarize_batch(batch)[1], str(error)


def test_failure_summary_removes_wrappers_and_keeps_full_reason(capsys):
    cause = ValueError(
        "未获取到影片数据 (IPX-658):\n"
        "  - r18dev: 本地数据库未找到唯一匹配的影片记录（可能未收录、番号不匹配或存在重复记录）；在线抓取已关闭"
    )
    stats, original = failure_stats(cause)
    print_summary(stats)
    output = capsys.readouterr().out
    assert "IPX-658: 抓取数据：r18dev: 本地库无唯一匹配（未收录/番号不符/多条记录）；在线抓取关闭" in output
    assert output.count("IPX-658") == 1
    assert "未获取到影片数据" not in output
    assert stats["failed_list"] == [("IPX-658", original)]


def test_failure_summary_keeps_multiple_sources_on_one_line(capsys):
    cause = ValueError("未获取到影片数据 (IPX-658):\n  - r18dev: 未收录\n  - javbus: 网络超时")
    stats, _ = failure_stats(cause)
    print_summary(stats)
    assert "IPX-658: 抓取数据：r18dev: 未收录；javbus: 网络超时" in capsys.readouterr().out


def test_failure_summary_displays_reason_longer_than_old_limit(capsys):
    reason = "单个目录或文件名超过文件系统限制 255: " + "中文目录" * 30
    stats, original = failure_stats(ValueError("无法生成符合长度限制的输出路径，请缩短输出目录或命名模板；" + reason), Stage.PLAN)
    print_summary(stats)
    output = capsys.readouterr().out
    assert "IPX-658: 规划输出：" + reason in output
    assert "请缩短输出目录或命名模板" not in output
    assert stats["failed_list"][0][1] == original


def test_failure_summary_bounds_long_paths_and_preserves_filename(capsys):
    filename = "IPX-658-poster.webp"
    path = "/media/" + "长目录/" * 100 + filename
    stats, original = failure_stats(OSError(36, "File name too long", path), Stage.PREPARE)
    print_summary(stats)
    line = next(line.strip() for line in capsys.readouterr().out.splitlines() if line.strip().startswith("IPX-658:"))
    summary = line.removeprefix("IPX-658: ")
    assert len(summary) == 240
    assert summary.startswith("准备产物：[Errno 36] File name too long: '/media/")
    assert " … " in summary
    assert summary.endswith(filename + "'")
    assert stats["failed_list"][0][1] == original


def test_failure_summary_does_not_hide_crawl_error_without_source_details(capsys):
    stats, _ = failure_stats(ValueError("未获取到影片数据 (IPX-658):\n"))
    print_summary(stats)
    output = capsys.readouterr().out
    assert "IPX-658: 抓取数据：未获取到影片数据" in output
    assert output.count("IPX-658") == 1
