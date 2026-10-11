"""批次资源限额、排队时间、共享池取消和翻译容量。"""

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image
from pydantic_extra_types.pendulum_dt import Duration

from javsp.concurrency import BatchCancelled, BatchResources
from javsp.config import Cfg, Concurrency, load_config
from javsp.crawl.dispatcher import CrawlerExecutor
from javsp.datatype import Movie
from javsp.publish import media
from javsp.translate import LocalTranslator
from javsp.workflow.batch import _Translator


def configuration(**limits):
    cfg = load_config([])
    return cfg.model_copy(update={
        "concurrency": Concurrency(**limits),
        "crawler": cfg.crawler.model_copy(update={"sleep_after_scraping": Duration(0), "online_enabled": True}),
        "r18dev": cfg.r18dev.model_copy(update={"enabled": False}),
    })


def test_config_defaults_old_files_and_override_priority(tmp_path, monkeypatch):
    cfg = load_config([])
    data = cfg.model_dump()
    del data["concurrency"]
    assert Cfg.model_validate(data).concurrency == Concurrency(movies=3, crawlers=6, images=3)
    path = tmp_path / "config.yml"
    path.write_text("concurrency:\n  movies: 2\n  images: 4\n", encoding="utf-8")
    monkeypatch.setenv("JAVSP_CONCURRENCY__MOVIES", "4")
    cfg = load_config(["-c", str(path), "--oconcurrency.movies", "5"])
    assert cfg.concurrency.movies == 5 and cfg.concurrency.images == 4 and cfg.concurrency.crawlers == 6


@pytest.mark.parametrize("field", ["movies", "crawlers", "images"])
@pytest.mark.parametrize("value", [0, -1, "bad"])
def test_invalid_concurrency(field, value):
    with pytest.raises(ValueError):
        Concurrency.model_validate({field: value})


def test_crawler_queue_time_does_not_consume_soft_deadline(monkeypatch):
    cfg = configuration(crawlers=1)
    cfg = cfg.model_copy(update={
        "crawler": cfg.crawler.model_copy(update={"selection": {"normal": ["slow", "fast"]}}),
        "network": cfg.network.model_copy(update={"retry": 1, "timeout": Duration(seconds=0.02)}),
    })
    started, release = threading.Event(), threading.Event()
    workers = []

    def slow(info, *, context):
        workers.append(threading.current_thread())
        started.set()
        assert release.wait(5)
        info.title = "late"

    def fast(info, *, context):
        workers.append(threading.current_thread())
        info.title = "in time"

    monkeypatch.setitem(sys.modules, "javsp.web.slow", SimpleNamespace(parse_data=slow))
    monkeypatch.setitem(sys.modules, "javsp.web.fast", SimpleNamespace(parse_data=fast))
    resources = BatchResources(cfg)
    with ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(CrawlerExecutor(cfg, resources=resources).crawl, Movie("ABC-123"))
        try:
            assert started.wait(5)
            time.sleep(0.08)
            assert not future.done()
        finally:
            release.set()
        result = future.result(timeout=5)
    resources.close()
    assert list(result.data) == ["fast"]
    assert ("slow", "抓取超时") in result.failures
    assert len(set(workers)) == 1 and all(not worker.is_alive() for worker in workers)


def test_site_tasks_serialize_and_cool_down():
    cfg = configuration(crawlers=3)
    interval = 0.03
    cfg = cfg.model_copy(update={"crawler": cfg.crawler.model_copy(update={"sleep_after_scraping": Duration(seconds=interval)})})
    resources = BatchResources(cfg)
    started, release, other_done = threading.Event(), threading.Event(), threading.Event()
    times = []

    def first():
        started.set()
        assert release.wait(5)
        times.append(time.monotonic())

    def second():
        times.append(time.monotonic())

    first_future = resources.submit(resources.crawlers, resources.site_task, "same", first, [None, None])
    assert started.wait(5)
    second_started = [None, None]
    second_future = resources.submit(resources.crawlers, resources.site_task, "same", second, second_started)
    other = resources.submit(resources.crawlers, resources.site_task, "other", other_done.set, [None, None])
    try:
        assert other_done.wait(5) and second_started[0] is None
    finally:
        release.set()
    for future in (first_future, second_future, other):
        future.result(timeout=5)
    resources.close()
    assert times[1] - times[0] >= interval


def test_shared_image_pool_limits_requests_and_is_reusable(tmp_path, monkeypatch):
    cfg = configuration(images=2)
    resources = BatchResources(cfg)
    barrier = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    active, maximum = 0, 0
    threads = set()

    def download(url, path, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            threads.add(threading.current_thread())
        try:
            if url in ("a", "b"):
                barrier.wait()
            Image.new("RGB", (12, 8), "blue").save(path, format="PNG")
            return {"elapsed": 1, "rate": 100}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(media, "download", download)
    with ThreadPoolExecutor(max_workers=2) as callers:
        futures = [callers.submit(media.download_cover, [url], str(tmp_path / f"{url}.jpg"), cfg=cfg, resources=resources)
                   for url in ("a", "b")]
        assert [future.result(timeout=5)[0] for future in futures] == ["a", "b"]
    assert media.download_cover(["c"], str(tmp_path / "c.jpg"), cfg=cfg, resources=resources)[0] == "c"
    resources.close()
    assert maximum == 2 and len(threads) == 2 and all(not thread.is_alive() for thread in threads)
    assert not list(tmp_path.glob(".javsp-*"))


def test_cancelled_queued_site_task_never_starts():
    resources = BatchResources(configuration(crawlers=2))
    started, release = threading.Event(), threading.Event()
    def first():
        started.set()
        assert release.wait(5)
    future = resources.submit(resources.crawlers, resources.site_task, "same", first, [None, None])
    assert started.wait(5)
    queued_started = [None, None]
    queued = resources.submit(resources.crawlers, resources.site_task, "same", lambda: pytest.fail("应停止"), queued_started)
    resources.stopped.set()
    release.set()
    future.result(timeout=5)
    with pytest.raises(BatchCancelled):
        queued.result(timeout=5)
    resources.close()
    assert queued_started[0] is None


@pytest.mark.parametrize("parallel", [1, 2])
def test_translation_capacity_and_session_isolation(monkeypatch, parallel):
    cfg = configuration()
    runtime = cfg.translator.runtime.model_copy(update={"parallel": parallel})
    cfg = cfg.model_copy(update={"translator": cfg.translator.model_copy(update={"runtime": runtime})})
    resources = BatchResources(cfg)
    local = LocalTranslator(cfg.translator)
    translator = _Translator(local, resources)
    active, maximum = 0, 0
    lock = threading.Lock()
    sessions = []
    barrier = threading.Barrier(parallel, timeout=5)

    def session_factory():
        nonlocal active, maximum
        session = MagicMock()
        session.__enter__.return_value = session
        sessions.append(session)
        def post(*args, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                barrier.wait()
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                    "choices": [{"message": {"content": "译文"}, "finish_reason": "stop"}],
                })
            finally:
                with lock:
                    active -= 1
        session.post.side_effect = post
        return session

    monkeypatch.setattr("javsp.translate.requests.Session", session_factory)
    with ThreadPoolExecutor(max_workers=4) as callers:
        futures = [callers.submit(translator.translate, "日本語", "title") for _ in range(4)]
        assert [future.result(timeout=5) for future in futures] == ["译文"] * 4
    local.close()
    resources.close()
    assert maximum == parallel and len(sessions) == 4
    assert all(session.trust_env is False and session.__exit__.called for session in sessions)


def test_fanart_interval_is_shared_across_movies(tmp_path, monkeypatch):
    cfg = configuration(images=2)
    resources = BatchResources(cfg)
    times = []
    lock = threading.Lock()

    def download(url, path, **kwargs):
        with lock:
            times.append(time.monotonic())
        Image.new("RGB", (12, 8), "blue").save(path, format="PNG")
        return {"elapsed": 1, "rate": 100}

    monkeypatch.setattr(media, "download", download)
    interval = 0.03
    with ThreadPoolExecutor(max_workers=2) as callers:
        futures = []
        for name in ("a", "b"):
            movie = SimpleNamespace(info=SimpleNamespace(preview_pics=["0", "1"]), save_dir=str(tmp_path / name))
            futures.append(callers.submit(media.download_extra_fanarts, movie, interval, cfg=cfg, resources=resources))
        assert all(future.result(timeout=5) == [] for future in futures)
    resources.close()
    assert len(times) == 4
    assert all(right - left >= interval - 0.002 for left, right in zip(times, times[1:]))
    assert len(list(tmp_path.rglob("*.png"))) == 4 and not list(tmp_path.rglob(".javsp-*"))


def test_repeated_cancellation_still_drains_shared_tasks(monkeypatch):
    from javsp import concurrency

    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    original = concurrency.wait

    def writer():
        started.set()
        assert release.wait(5)
        finished.set()

    def interrupted_wait(*args, **kwargs):
        calls.append(finished.is_set())
        if len(calls) <= 2:
            raise KeyboardInterrupt()
        release.set()
        return original(*args, **kwargs)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        assert started.wait(5)
        monkeypatch.setattr(concurrency, "wait", interrupted_wait)
        try:
            with pytest.raises(KeyboardInterrupt):
                concurrency.await_futures([future])
        finally:
            release.set()
        assert future.done() and finished.is_set() and calls[:2] == [False, False]
