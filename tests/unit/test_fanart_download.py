from javsp.config import load_config

"""离线验证剧照并发、统一限速、编号及失败处理。"""

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from PIL import Image

from javsp.publish import processor


def movie_for(tmp_path, urls):
    return SimpleNamespace(save_dir=str(tmp_path), info=SimpleNamespace(preview_pics=urls))


def save_image(path, value):
    Image.new("RGB", (12, 8), (value, 0, 0)).save(path, format="PNG")
    return {"elapsed": 1, "rate": 100}


def test_all_images_keep_original_numbers_with_at_most_three_downloads(tmp_path, monkeypatch):
    lock = threading.Lock()
    three_started = threading.Event()
    release = threading.Event()
    active = 0
    max_active = 0
    calls = []
    paths = []
    progress = []
    errors = []

    def download(url, path, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(active, max_active)
            calls.append(url)
            paths.append(path)
            if active == 3:
                three_started.set()
        try:
            assert release.wait(5)
            return save_image(path, int(url))
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(processor, "download", download)
    movie = movie_for(tmp_path, [str(i) for i in range(8)])
    bar = SimpleNamespace(set_description=progress.append)

    def run():
        try:
            processor.download_extra_fanarts(movie, 0, bar, cfg=load_config())
        except Exception as error:
            errors.append(error)

    caller = threading.Thread(target=run)
    caller.start()
    try:
        assert three_started.wait(5)
        assert max_active == 3
        assert caller.is_alive()  # 全部任务结束前不能返回。
    finally:
        release.set()
        caller.join(5)
        assert not caller.is_alive()
    assert not errors
    assert max_active == 3
    assert sorted(calls) == movie.info.preview_pics
    assert len(set(paths)) == 8
    assert progress[-1] == "下载剧照 8/8"
    for idx in range(8):
        with Image.open(tmp_path / "extrafanart" / f"{idx}.png") as image:
            assert image.getpixel((0, 0)) == (idx, 0, 0)
    assert not list((tmp_path / "extrafanart").glob(".javsp-fanart-*"))


def test_start_interval_is_shared_and_downloads_overlap(tmp_path, monkeypatch):
    lock = threading.Lock()
    starts = []
    active = 0
    max_active = 0

    def download(url, path, **kwargs):
        nonlocal active, max_active
        with lock:
            starts.append(time.monotonic())
            active += 1
            max_active = max(active, max_active)
        try:
            time.sleep(0.1)
            return save_image(path, 1)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(processor, "download", download)
    processor.download_extra_fanarts(movie_for(tmp_path, ["a", "b", "c", "d"]), 0.04, cfg=load_config())
    starts.sort()
    assert len(starts) == 4
    assert all(after - before >= 0.035 for before, after in zip(starts, starts[1:]))
    assert 2 <= max_active <= 3


def test_single_image_has_no_trailing_sleep(tmp_path, monkeypatch):
    def sleep(seconds):
        raise AssertionError(f"单张图片不应等待: {seconds}")

    clock = SimpleNamespace(monotonic=time.monotonic, sleep=sleep, strftime=time.strftime, gmtime=time.gmtime)
    monkeypatch.setattr(processor, "time", clock)
    monkeypatch.setattr(processor, "download", lambda url, path, **kw: save_image(path, 1))
    processor.download_extra_fanarts(movie_for(tmp_path, ["one"]), 1.5, cfg=load_config())
    assert (tmp_path / "extrafanart" / "0.png").exists()


def test_failed_and_invalid_images_do_not_block_others_or_replace_existing_files(tmp_path, monkeypatch, caplog):
    destination = tmp_path / "extrafanart"
    destination.mkdir()
    (destination / "0.png").write_bytes(b"existing")

    def download(url, path, **kwargs):
        if url == "failed":
            Path(path).write_bytes(b"partial")
            raise requests.Timeout("timeout")
        if url == "invalid":
            Path(path).write_bytes(b"not an image")
            return {}
        return save_image(path, 2)

    monkeypatch.setattr(processor, "download", download)
    processor.download_extra_fanarts(movie_for(tmp_path, ["failed", "invalid", "good"]), 0, cfg=load_config())
    assert (destination / "0.png").read_bytes() == b"existing"
    assert not (destination / "1.png").exists()
    assert (destination / "2.png").exists()
    assert "下载剧照0失败" in caplog.text
    assert "下载剧照1失败" in caplog.text
    assert not list(destination.glob(".javsp-fanart-*"))


def test_no_images_creates_no_directory(tmp_path):
    processor.download_extra_fanarts(movie_for(tmp_path, []), 1.5, cfg=load_config())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("image_format,extension", [("JPEG", ".jpg"), ("PNG", ".png"), ("WEBP", ".webp"), ("GIF", ".gif")])
def test_fanart_extension_matches_content(tmp_path, monkeypatch, image_format, extension):
    original = []

    def download(url, path, **kwargs):
        Image.new("RGB", (12, 8), "blue").save(path, format=image_format)
        original.append(Path(path).read_bytes())
        return {"elapsed": 1, "rate": 100}

    monkeypatch.setattr(processor, "download", download)
    processor.download_extra_fanarts(movie_for(tmp_path, ["https://example.test/wrong.png"]), 0, cfg=load_config())
    target = tmp_path / "extrafanart" / ("0" + extension)
    assert target.read_bytes() == original[0]
    with Image.open(target) as image:
        assert image.format == image_format
    assert sorted(path.name for path in target.parent.iterdir()) == [target.name]
