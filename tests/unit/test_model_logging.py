"""模型日志的级别、管道排空和进程清理回归测试。"""

import io
import json
import logging
import subprocess
import sys
from unittest.mock import MagicMock

import pytest
import requests

from javsp.config import Translator
from javsp.model_logging import ModelLogReader
from javsp.translate import LocalTranslator, TranslateError


def _json_line(level, message):
    return json.dumps({"type": "log", "level": level, "msg": message}) + "\n"


@pytest.mark.parametrize("debug", [False, True])
def test_model_details_follow_application_debug(debug, caplog):
    stream = io.StringIO(
        "0.15.702.028 I slot get_availabl: selected slot by LRU\n"
        "0.31.022.309 I slot print_timing: prompt eval time = 8600.52 ms\n"
        + _json_line("info", "llama_model_loader: loading model\nmodel size = 4 GiB\n")
        + _json_line("warn", "warning context")
        + _json_line("error", "error context")
    )
    with caplog.at_level(logging.DEBUG if debug else logging.INFO, logger="javsp.translate.model"):
        reader = ModelLogReader()
        reader.start(stream)
        reader.close()
    assert ("get_availabl" in caplog.text) == debug
    assert ("print_timing" in caplog.text) == debug
    assert ("model size" in caplog.text) == debug
    assert "warning context" in caplog.text
    assert "error context" in caplog.text
    assert [record.levelno for record in caplog.records if "context" in record.message] == [logging.WARNING, logging.ERROR]
    assert stream.closed


def test_plaintext_levels_colors_and_invalid_utf8_survive(caplog):
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stderr.buffer.write(b'\\x1b[31mE backend failed\\x1b[0m\\xff\\n')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    reader = ModelLogReader()
    reader.start(process.stdout)
    try:
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        reader.close()
    assert "backend failed�" in caplog.text
    assert "\x1b" not in caplog.text
    assert caplog.records[-1].levelno == logging.ERROR


def test_reader_drains_large_stdout_and_stderr_with_bounded_tail(caplog):
    script = """
import json, sys
for index in range(5000):
    print(json.dumps({'type': 'log', 'level': 'info', 'msg': 'slot ' + str(index) + 'x' * 100}))
    print('loader: metadata ' + str(index), file=sys.stderr)
print(json.dumps({'type': 'log', 'level': 'error', 'msg': 'final failure'}))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8"
    )
    reader = ModelLogReader()
    reader.start(process.stdout)
    try:
        assert process.wait(timeout=5) == 0  # 输出远超过管道容量，未排空时会堵塞。
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        reader.close()
    assert "final failure" in caplog.text
    assert "slot" not in caplog.text
    assert len(reader.diagnostic.split(" | ")) == 10
    assert reader.diagnostic.endswith("final failure")
    assert process.stdout.closed
    reader.close()  # 重复关闭安全。


def test_tail_bounds_long_messages_and_ignores_nonlog_events(caplog):
    reader = ModelLogReader()
    reader.start(
        io.StringIO(
            _json_line("info", "x" * 2000) + json.dumps({"type": "request", "data": {"prompt": "private-content"}}) + "\n\n"
        )
    )
    reader.close()
    assert reader.diagnostic == "x" * 1000
    assert "private-content" not in caplog.text


def test_warning_continuations_and_malformed_records_do_not_break_reader(caplog):
    stream = io.StringIO(
        _json_line("warn", "context too small")
        + _json_line("cont", "  details for warning")
        + json.dumps({"type": "log", "level": [], "msg": "unexpected level"})
        + "\n"
        + _json_line("error", "still reading")
    )
    reader = ModelLogReader()
    reader.start(stream)
    reader.close()
    assert [(record.levelno, record.message) for record in caplog.records] == [
        (logging.WARNING, "context too small"),
        (logging.WARNING, "details for warning"),
        (logging.ERROR, "still reading"),
    ]


def test_model_console_uses_application_format_and_stdout():
    script = """
import io, json
from javsp.logging_config import configure_logging
from javsp.model_logging import ModelLogReader
configure_logging()
reader = ModelLogReader()
reader.start(io.StringIO(json.dumps({'type': 'log', 'level': 'info', 'msg': 'hidden slot'}) + '\\n'
                       + json.dumps({'type': 'log', 'level': 'warn', 'msg': 'visible warning'}) + '\\n'))
reader.close()
configure_logging(debug=True)
reader.start(io.StringIO('0.31.022.309 I slot print_timing: eval time = 6719.67 ms\\n'))
reader.close()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=10)
    assert result.stderr == ""
    lines = result.stdout.splitlines()
    assert len(lines) == 2
    assert "WARNING:javsp.translate.model: visible warning" in lines[0]
    assert "DEBUG:javsp.translate.model: slot print_timing: eval time = 6719.67 ms" in lines[1]
    assert "0.31.022.309" not in result.stdout


@pytest.mark.parametrize(
    ("line", "level"),
    [
        ("0.31.022.309 W backend warning", logging.WARNING),
        ("error: unknown argument", logging.ERROR),
        ("loader: warning: using fallback", logging.WARNING),
    ],
)
def test_unstructured_diagnostics_keep_severity(line, level, caplog):
    reader = ModelLogReader()
    reader.start(io.StringIO(line + "\n"))
    reader.close()
    assert caplog.records[-1].levelno == level


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setattr("javsp.translate.ensure_model", lambda *args, **kwargs: tmp_path / "test.gguf")
    session = MagicMock()
    session.get.return_value.ok = True
    session.get.return_value.json.return_value = {"status": "ok"}
    process = MagicMock()
    process.poll.return_value = None
    process.stdout = io.StringIO(_json_line("error", "cannot load model"))
    popen = MagicMock(return_value=process)
    monkeypatch.setattr("javsp.translate.subprocess.Popen", popen)
    translator = LocalTranslator(Translator(), binary="llama-server", session=session)
    return translator, process, popen, session


def test_runtime_routes_both_streams_and_logs_lifecycle(runtime, caplog):
    translator, process, popen, _ = runtime
    with caplog.at_level(logging.INFO, logger="javsp.translate"):
        with translator:
            command = popen.call_args.args[0]
            assert "--log-jsonl" in command
            assert command[command.index("--log-verbosity") + 1] == "3"
            assert popen.call_args.kwargs["stdout"] == subprocess.PIPE
            assert popen.call_args.kwargs["stderr"] == subprocess.STDOUT
            assert popen.call_args.kwargs["errors"] == "replace"
    assert "启动本地翻译服务" in caplog.text
    assert "本地翻译服务已就绪" in caplog.text
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=10)
    assert process.stdout.closed


def test_early_exit_collects_final_diagnostics_and_closes(runtime):
    translator, process, _, session = runtime
    process.poll.return_value = 7
    process.returncode = 7
    with pytest.raises(TranslateError, match="状态码 7.*模型日志末尾: cannot load model"):
        translator.start()
    process.terminate.assert_not_called()
    session.get.assert_not_called()
    assert process.stdout.closed
    assert translator.process is None


def test_readiness_timeout_kills_then_drains_and_preserves_error(runtime, monkeypatch):
    translator, process, _, _ = runtime
    monkeypatch.setattr("javsp.translate.time.monotonic", MagicMock(side_effect=[0, 601]))
    process.wait.side_effect = [subprocess.TimeoutExpired("llama-server", 10), 0]
    with pytest.raises(TranslateError, match="就绪超时.*cannot load model"):
        translator.start()
    process.kill.assert_called_once()
    assert process.wait.call_count == 2
    assert process.stdout.closed


def test_cancel_during_start_closes_pipe_and_preserves_cancellation(runtime):
    translator, process, _, session = runtime
    session.get.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        translator.start()
    process.terminate.assert_called_once()
    assert process.stdout.closed


def test_start_failure_closes_owned_session(monkeypatch, tmp_path):
    monkeypatch.setattr("javsp.translate.ensure_model", lambda *args, **kwargs: tmp_path / "test.gguf")
    session = MagicMock()
    monkeypatch.setattr("javsp.translate.requests.Session", lambda: session)
    monkeypatch.setattr("javsp.translate.subprocess.Popen", MagicMock(side_effect=OSError("launch failed")))
    translator = LocalTranslator(Translator(), binary="llama-server")
    with pytest.raises(TranslateError, match="启动 llama-server 失败: launch failed"):
        translator.start()
    session.close.assert_called_once()


def test_health_request_failure_still_times_out_with_model_context(runtime, monkeypatch):
    translator, process, _, session = runtime
    session.get.side_effect = requests.ConnectionError("not ready")
    monkeypatch.setattr("javsp.translate.time.monotonic", MagicMock(side_effect=[0, 1, 601]))
    monkeypatch.setattr("javsp.translate.time.sleep", lambda _: None)
    with pytest.raises(TranslateError, match="就绪超时.*cannot load model"):
        translator.start()
    assert process.stdout.closed
