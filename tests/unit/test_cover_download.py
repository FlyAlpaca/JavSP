"""离线验证封面并发下载、高清优先和临时文件隔离。"""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from PIL import Image

from javsp import processor


@pytest.fixture
def cover_config(monkeypatch):
    monkeypatch.setattr(processor, "Cfg", lambda: SimpleNamespace(network=SimpleNamespace(retry=2)))


def save_image(path, color):
    Image.new("RGB", (12, 8), color).save(path, format="PNG")
    return {"elapsed": 1, "rate": 100}


@pytest.mark.parametrize("highres", [True, False])
def test_fast_cover_waits_for_slow_request_and_loser_cannot_overwrite(tmp_path, monkeypatch, cover_config, highres):
    slow_started = threading.Event()
    release_slow = threading.Event()
    workers = []
    paths = []
    calls = []

    def download(url, path):
        calls.append(url)
        paths.append(path)
        workers.append(threading.current_thread())
        if url == "slow.jpg":
            slow_started.set()
            assert release_slow.wait(5)
            return save_image(path, "red")
        assert slow_started.wait(5)
        return save_image(path, "blue")

    monkeypatch.setattr(processor, "download", download)
    target = tmp_path / "fanart.jpg"
    results = []
    candidates = ["slow.jpg", "fast.png"]
    caller = threading.Thread(
        target=lambda: results.append(
            processor.download_cover(
                ["ordinary.jpg"] if highres else candidates,
                str(target),
                candidates if highres else None,
            )
        )
    )
    caller.start()
    try:
        assert slow_started.wait(5)
        # 等待胜出图片发布，调用仍须回收在途请求。
        import time

        deadline = time.monotonic() + 5
        while not (tmp_path / "fanart.png").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (tmp_path / "fanart.png").exists()
        assert caller.is_alive()
    finally:
        release_slow.set()
        caller.join(5)
    assert not caller.is_alive()
    result = results[0]
    assert result == ("fast.png", str(tmp_path / "fanart.png"))
    assert "ordinary.jpg" not in calls
    assert len(set(paths)) == 2
    assert all(not worker.is_alive() for worker in workers)
    with Image.open(result[1]) as image:
        assert image.getpixel((0, 0)) == (0, 0, 255)
    assert not target.exists()
    assert not list(tmp_path.glob(".javsp-cover-*"))


def test_highres_failures_complete_before_ordinary_fallback(tmp_path, monkeypatch, cover_config):
    failures = set()
    calls = []
    lock = threading.Lock()

    def download(url, path):
        calls.append(url)
        if url.startswith("hd"):
            Path(path).write_bytes(b"invalid image")
            with lock:
                failures.add(url)
            if url == "hd404.jpg":
                response = requests.Response()
                response.status_code = 404
                raise requests.HTTPError(response=response)
            return {}
        assert failures == {"hd404.jpg", "hd-invalid.jpg"}
        return save_image(path, "green")

    monkeypatch.setattr(processor, "download", download)
    target = str(tmp_path / "fanart.jpg")
    assert processor.download_cover(["ordinary.jpg"], target, ["hd404.jpg", "hd-invalid.jpg"]) == (
        "ordinary.jpg",
        target,
    )
    assert calls.count("hd404.jpg") == 1
    assert not list(tmp_path.glob(".javsp-cover-*"))


def test_all_failed_preserves_existing_cover_and_reports_retries(tmp_path, monkeypatch, cover_config):
    calls = []

    def download(url, path):
        calls.append(url)
        Path(path).write_bytes(b"partial download")
        raise requests.Timeout("timeout")

    monkeypatch.setattr(processor, "download", download)
    target = tmp_path / "fanart.jpg"
    target.write_bytes(b"existing cover")
    result, reason = processor.download_cover(["broken.jpg", "broken.jpg"], str(target), movie_id="ABC-123")
    assert result is None
    assert "broken.jpg" in reason and "Timeout" in reason and "重试2次" in reason
    assert calls == ["broken.jpg", "broken.jpg"]
    assert target.read_bytes() == b"existing cover"
    assert not list(tmp_path.glob(".javsp-cover-*"))


def test_ordinary_http_error_retries(tmp_path, monkeypatch, cover_config):
    calls = []

    def download(url, path):
        calls.append(url)
        if len(calls) == 1:
            raise requests.HTTPError("temporary error")
        return save_image(path, "blue")

    monkeypatch.setattr(processor, "download", download)
    target = str(tmp_path / "fanart.jpg")
    assert processor.download_cover(["cover.jpg"], target) == ("cover.jpg", target)
    assert len(calls) == 2
    assert not list(tmp_path.glob(".javsp-cover-*"))


def test_parallel_downloads_are_limited_to_three(tmp_path, monkeypatch, cover_config):
    three_started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    workers = set()
    active = 0
    max_active = 0
    results = []

    def download(url, path):
        nonlocal active, max_active
        with lock:
            workers.add(threading.current_thread())
            active += 1
            max_active = max(max_active, active)
            if active == 3:
                three_started.set()
        try:
            assert release.wait(5)
            return save_image(path, "blue")
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(processor, "download", download)
    target = str(tmp_path / "fanart.jpg")
    caller = threading.Thread(
        target=lambda: results.append(
            processor.download_cover([f"cover-{i}.jpg" for i in range(8)], target),
        )
    )
    caller.start()
    try:
        assert three_started.wait(5)
        assert max_active == 3
    finally:
        release.set()
        caller.join(5)
        assert not caller.is_alive()
        for worker in list(workers):
            worker.join(5)
            assert not worker.is_alive()
    assert results and results[0][0] is not None
    assert max_active == 3
    assert not list(tmp_path.glob(".javsp-cover-*"))


def test_no_cover_addresses(tmp_path, cover_config):
    assert processor.download_cover([], str(tmp_path / "fanart.jpg")) == (None, "无可用封面地址")
    assert list(tmp_path.iterdir()) == []
