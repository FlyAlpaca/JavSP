"""验证批处理失败可被自动化识别。"""

import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from javsp import __main__ as cli
from javsp import func
from javsp.config import Cfg
from javsp.datatype import MovieInfo
from javsp.workflow.models import JobError, JobResult, OutputResult, ScanResult, Stage


@pytest.mark.parametrize("failed, code", [(0, 0), (1, 1), (3, 1)])
def test_entry_returns_failure_status(tmp_path, monkeypatch, failed, code):
    cfg = SimpleNamespace(
        other=SimpleNamespace(debug=False, check_update=False, auto_update=False, auto_exit=True, interactive=False),
        scanner=SimpleNamespace(input_directory=str(tmp_path), manual=False),
    )
    monkeypatch.setattr(cli, "Cfg", lambda: cfg)
    for name in ("load_actress_aliases", "check_update", "import_crawlers", "setup_filemove_log", "print_summary"):
        monkeypatch.setattr(cli, name, lambda *a, **kw: None)
    monkeypatch.setattr(cli, "get_scan_dir", lambda *a: str(tmp_path))
    monkeypatch.setattr(cli, "Pipeline", lambda cfg: SimpleNamespace(scan=lambda root: ScanResult([object()] * 3)))
    monkeypatch.setattr(cli, "RunNormalMode", lambda *a: ([], {"failed": failed}))
    monkeypatch.setattr(cli.time, "sleep", lambda *a: pytest.fail("非交互模式不应等待退出"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.entry()
    assert exc.value.code == code


def test_entry_with_missing_noninteractive_directory_exits_without_prompt(tmp_path, monkeypatch):
    cfg = SimpleNamespace(
        other=SimpleNamespace(debug=False, check_update=False, auto_update=False, interactive=False),
        scanner=SimpleNamespace(input_directory=tmp_path / "missing"),
    )
    monkeypatch.setattr(cli, "Cfg", lambda: cfg)
    monkeypatch.setattr(cli, "load_actress_aliases", lambda: None)
    monkeypatch.setattr(cli, "check_update", lambda *args: None)
    monkeypatch.setattr(func, "prompt", lambda *args: pytest.fail("非交互模式不应提示选目录"))
    with pytest.raises(SystemExit) as exc:
        cli.entry()
    assert exc.value.code == 1


def test_invalid_config_without_terminal_exits_without_prompt(monkeypatch):
    with pytest.raises(ValidationError) as exc:
        Cfg.model_validate({})
    error = exc.value

    def invalid_config():
        raise error

    monkeypatch.setattr(cli, "Cfg", invalid_config)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("无终端时不应等待输入"))
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
    movies = [cli.Movie("ABC-123"), cli.Movie("ABC-124")]
    visited = []

    def run(self, movie):
        visited.append(movie)
        if movie is movies[0]:
            return JobResult(movie, error=JobError("ABC-123", Stage.COMMIT, OSError("move failed")))
        movie.info = MovieInfo(movie)
        return JobResult(movie, output=OutputResult(movie))

    monkeypatch.setattr(cli.JobRunner, "run", run)
    monkeypatch.setattr("javsp.crawl.dispatcher.CrawlerExecutor.throttle", lambda self: None)
    successful, stats = cli.RunNormalMode(movies, cfg)
    assert visited == movies
    assert successful == [movies[1]]
    assert stats["total"] == 2 and stats["failed"] == 1 and stats["success"] == 1
    assert "move failed" in stats["failed_list"][0][1]
