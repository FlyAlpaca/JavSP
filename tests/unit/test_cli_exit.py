"""验证批处理失败可被自动化识别。"""

from types import SimpleNamespace

import pytest

from javsp import __main__ as cli


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
    monkeypatch.setattr(cli, "scan_movies", lambda *a: [object()] * 3)
    monkeypatch.setattr(cli, "RunNormalMode", lambda *a: ([], {"failed": failed}))
    monkeypatch.setattr(cli.time, "sleep", lambda *a: None)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.entry()
    assert exc.value.code == code


def test_batch_continues_and_counts_failure(monkeypatch):
    cfg = SimpleNamespace(
        translator=SimpleNamespace(engine=None),
        summarizer=SimpleNamespace(extra_fanarts=SimpleNamespace(enabled=False), move_files=False),
        crawler=SimpleNamespace(sleep_after_scraping=cli.Duration(0)),
    )
    movies = [cli.Movie("ABC-123"), cli.Movie("ABC-124")]
    visited = []

    def process(movie, *args):
        visited.append(movie)
        if movie is movies[0]:
            raise OSError("move failed")

    monkeypatch.setattr(cli, "_process_single_movie", process)
    successful, stats = cli.RunNormalMode(movies, cfg)
    assert visited == movies
    assert successful == [movies[1]]
    assert stats["total"] == 2 and stats["failed"] == 1 and stats["success"] == 1
    assert stats["failed_list"] == [("ABC-123", "move failed")]
