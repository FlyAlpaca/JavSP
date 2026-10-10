"""离线验证调度超时、结果隔离和工作线程回收。"""

import sys
import threading
import time
from types import SimpleNamespace

import pytest
import requests

from javsp import dispatcher
from javsp.datatype import Movie


@pytest.mark.parametrize("raise_timeout", [False, True])
def test_timeout_drains_worker_and_prevents_retries(monkeypatch, raise_timeout):
    selection = {"normal": ["testslow", "testfast"]}
    cfg = SimpleNamespace(
        crawler=SimpleNamespace(selection=selection),
        network=SimpleNamespace(retry=2, timeout=SimpleNamespace(total_seconds=lambda: 0.03)),
    )
    monkeypatch.setattr(dispatcher, "Cfg", lambda: cfg)
    slow_started, release, fast_done = threading.Event(), threading.Event(), threading.Event()
    calls, workers, results = [], [], []

    def slow(info):
        calls.append("slow")
        workers.append(threading.current_thread())
        slow_started.set()
        assert release.wait(5)
        info.title = "late title"
        if raise_timeout:
            raise requests.Timeout("late error")

    def fast(info):
        workers.append(threading.current_thread())
        info.title = "complete title"
        fast_done.set()

    for name, parser in (("testslow", slow), ("testfast", fast)):
        monkeypatch.setitem(sys.modules, f"javsp.web.{name}", SimpleNamespace(parse_data=parser))
    caller = threading.Thread(target=lambda: results.append(dispatcher.parallel_crawler(Movie("ABC-123"))))
    caller.start()
    try:
        assert slow_started.wait(5) and fast_done.wait(5)
        time.sleep(0.15)
        assert caller.is_alive()
    finally:
        release.set()
        caller.join(5)
    assert not caller.is_alive()
    assert calls == ["slow"]
    assert all(not worker.is_alive() for worker in workers)
    assert list(results[0]) == ["testfast"]
    assert results[0]["testfast"].title == "complete title"


def test_all_timed_out_reports_failure_and_drains(monkeypatch):
    cfg = SimpleNamespace(
        crawler=SimpleNamespace(selection={"normal": ["testslow"]}),
        network=SimpleNamespace(retry=1, timeout=SimpleNamespace(total_seconds=lambda: 0.01)),
    )
    monkeypatch.setattr(dispatcher, "Cfg", lambda: cfg)
    workers = []

    def slow(info):
        workers.append(threading.current_thread())
        time.sleep(0.06)
        info.title = "late title"

    monkeypatch.setitem(sys.modules, "javsp.web.testslow", SimpleNamespace(parse_data=slow))
    with pytest.raises(Exception, match="抓取超时"):
        dispatcher.parallel_crawler(Movie("ABC-123"))
    assert all(not worker.is_alive() for worker in workers)


def test_startup_error_drains_already_started_workers(monkeypatch):
    cfg = SimpleNamespace(
        crawler=SimpleNamespace(selection={"normal": ["testslow", "missing"]}),
        network=SimpleNamespace(retry=1, timeout=SimpleNamespace(total_seconds=lambda: 1)),
    )
    monkeypatch.setattr(dispatcher, "Cfg", lambda: cfg)
    workers = []
    started = threading.Event()

    def slow(info):
        workers.append(threading.current_thread())
        started.set()
        time.sleep(0.06)

    monkeypatch.setitem(sys.modules, "javsp.web.testslow", SimpleNamespace(parse_data=slow))
    monkeypatch.delitem(sys.modules, "javsp.web.missing", raising=False)
    with pytest.raises(KeyError):
        dispatcher.parallel_crawler(Movie("ABC-123"))
    # 首个线程也可能在停止信号发出后才开始执行，此时不应启动请求。
    assert all(not worker.is_alive() for worker in workers)
