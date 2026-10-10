"""验证批处理失败可被自动化识别。"""

import subprocess
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from javsp import __main__ as cli
from javsp.config import Cfg
from javsp.datatype import Movie, MovieInfo
from javsp.workflow.models import JobError, JobResult, OutputResult, ScanResult, Stage


@pytest.mark.parametrize("failed, code", [(0, 0), (1, 1), (3, 1)])
def test_entry_returns_failure_status(tmp_path, monkeypatch, failed, code):
    cfg = SimpleNamespace(
        other=SimpleNamespace(debug=False),
        scanner=SimpleNamespace(input_directory=str(tmp_path)),
        translator=object(),
    )
    monkeypatch.setattr(cli, "Cfg", lambda: cfg)
    for name in ("setup_filemove_log", "print_summary"):
        monkeypatch.setattr(cli, name, lambda *a, **kw: None)
    monkeypatch.setattr(cli, "startup_context", lambda cfg: nullcontext((str(tmp_path), object())))
    monkeypatch.setattr(cli, "Pipeline", lambda cfg, **kw: SimpleNamespace(scan=lambda root: ScanResult([object()] * 3)))
    monkeypatch.setattr(cli, "RunNormalMode", lambda *a: ([], {"failed": failed}))
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
    monkeypatch.setattr(cli, "Cfg", lambda: cfg)
    monkeypatch.setattr(cli, "startup_context", lambda cfg: (_ for _ in ()).throw(cli.StartupError("扫描目录不存在")))
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("容器批处理不应提示选目录"))
    with pytest.raises(SystemExit) as exc:
        cli.entry()
    assert exc.value.code == 1


def test_model_startup_failure_precedes_media_scan(monkeypatch):
    cfg = SimpleNamespace(translator=object(), other=SimpleNamespace(debug=False))
    monkeypatch.setattr(cli, "Cfg", lambda: cfg)

    class FailedTranslator:
        def __enter__(self):
            raise RuntimeError("model unavailable")

        def __exit__(self, *_):
            pass

    monkeypatch.setattr("javsp.startup.prepare_startup", lambda _: "/media")
    monkeypatch.setattr("javsp.startup.LocalTranslator", lambda _: FailedTranslator())
    monkeypatch.setattr(cli, "Pipeline", lambda *_args, **_kwargs: pytest.fail("不应扫描媒体目录"))
    with pytest.raises(RuntimeError, match="model unavailable"):
        cli.entry()


def test_invalid_config_exits_without_prompt(monkeypatch):
    with pytest.raises(ValidationError) as exc:
        Cfg.model_validate({})
    error = exc.value

    def invalid_config():
        raise error

    monkeypatch.setattr(cli, "Cfg", invalid_config)
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
    cfg = Cfg()
    movies = [Movie("ABC-123"), Movie("ABC-124")]
    visited = []

    def run(self, movie):
        visited.append(movie)
        if movie is movies[0]:
            return JobResult(movie, error=JobError("ABC-123", Stage.COMMIT, OSError("move failed")))
        movie.info = MovieInfo(movie)
        return JobResult(movie, output=OutputResult(movie))

    monkeypatch.setattr(cli.JobRunner, "run", run)
    monkeypatch.setattr("javsp.translate.LocalTranslator.start", lambda self: self)
    monkeypatch.setattr("javsp.crawl.dispatcher.CrawlerExecutor.throttle", lambda self: None)
    successful, stats = cli.RunNormalMode(movies, cfg)
    assert visited == movies
    assert successful == [movies[1]]
    assert stats["total"] == 2 and stats["failed"] == 1 and stats["success"] == 1
    assert "move failed" in stats["failed_list"][0][1]
