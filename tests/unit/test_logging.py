"""验证控制台和文件日志各自的输出边界。"""

import logging
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

from javsp import datatype
from javsp.cli import ProgressDisplay
from javsp.workflow.models import Stage, StageEvent


def test_console_logging_respects_debug_and_is_idempotent():
    script = """
import logging
from javsp.logging_config import configure_logging
configure_logging()
logging.getLogger('check').debug('hidden')
logging.getLogger('check').info('normal')
configure_logging(debug=True)
logging.getLogger('check').debug('details')
configure_logging()
logging.getLogger('check').info('once')
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert result.stderr == ""
    assert "hidden" not in result.stdout
    assert len(result.stdout.splitlines()) == 3
    assert re.search(r"\d{4}-\d{2}-\d{2} .*\.\d{3} INFO:check: normal", result.stdout)
    assert "DEBUG:check: details" in result.stdout
    assert result.stdout.count("INFO:check: once") == 1


def test_filemove_details_do_not_repeat_on_console(tmp_path, monkeypatch, caplog):
    logger = logging.getLogger("test.filemove")
    monkeypatch.setattr(datatype, "filemove_logger", logger)
    cfg = SimpleNamespace(summarizer=SimpleNamespace(filemove_log=True))
    try:
        datatype.setup_filemove_log(cfg, log_dir=tmp_path)
        datatype.setup_filemove_log(cfg, log_dir=tmp_path)
        with caplog.at_level(logging.DEBUG):
            logger.debug("移动详情")
        assert logger.propagate is False
        assert "移动详情" not in caplog.text
        assert (tmp_path / "filemove.log").read_text(encoding="utf-8").count("移动详情") == 1
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()


@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("task_id", ["", "7"])
def test_nonterminal_progress_keeps_start_and_debug_stages(debug, task_id, caplog):
    display = ProgressDisplay()
    display.terminal = False
    events = [StageEvent("ABC-123", stage, task_id=task_id) for stage in (
        Stage.CRAWL, Stage.MERGE, Stage.TRANSLATE, Stage.PLAN, Stage.PREPARE, Stage.COMMIT, Stage.DONE,
    )]
    with caplog.at_level(logging.DEBUG if debug else logging.INFO):
        for event in events:
            display.on_event(event)
    assert [record.message for record in caplog.records if record.levelno == logging.INFO] == [
        ("任务 7 " if task_id else "") + "[ABC-123] 开始整理",
    ]
    assert ("提交产物" in caplog.text) == debug
    assert ("整理完成" in caplog.text) == debug
    assert not display.active
