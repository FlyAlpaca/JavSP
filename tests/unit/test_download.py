"""离线验证流式下载、准确进度及响应资源回收。"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from javsp.web import base


class StreamingResponse:
    def __init__(self, chunks, size=None, error=None):
        self.chunks = chunks
        self.headers = CaseInsensitiveDict({"Content-Length": str(size)} if size is not None else {})
        self.error = error
        self.closed = False

    @property
    def content(self):
        raise AssertionError("下载不应预先加载完整响应")

    def raise_for_status(self):
        if isinstance(self.error, requests.HTTPError):
            raise self.error

    def iter_content(self, chunk_size):
        assert chunk_size == 64 * 1024
        yield from self.chunks
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


@pytest.fixture
def network_config(monkeypatch):
    config = SimpleNamespace(network=SimpleNamespace(proxy_server=None, timeout=timedelta(seconds=2)))
    monkeypatch.setattr(base, "Cfg", lambda: config)


def install_response(monkeypatch, response):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(base.requests, "get", get)
    return calls


@pytest.mark.parametrize("size", [65543, None])
def test_download_streams_without_explicit_flush_and_reports_exact_bytes(tmp_path, monkeypatch, network_config, size):
    response = StreamingResponse([b"a" * 65536, b"", b"b" * 7], size=size)
    calls = install_response(monkeypatch, response)
    progress = []
    target = tmp_path / "download.bin"
    real_open = open

    class BufferedFile:
        def __enter__(self):
            self.file = real_open(target, "wb")
            return self

        def write(self, chunk):
            return self.file.write(chunk)

        def flush(self):
            raise AssertionError("不能逐块强制 flush")

        def __exit__(self, *args):
            self.file.close()

    monkeypatch.setattr(base, "open", lambda *args: BufferedFile(), raising=False)
    base.urlretrieve("https://example.test/file", target, lambda *args: progress.append(args), {"referer": "https://example.test/"})
    assert target.read_bytes() == b"a" * 65536 + b"b" * 7
    assert response.closed
    assert calls[0][1]["stream"] is True
    assert calls[0][1]["timeout"] == 6
    assert calls[0][1]["headers"]["referer"] == "https://example.test/"
    assert progress == [(0, 1, size or -1), (65536, 1, size or -1), (65543, 1, size or -1)]


@pytest.mark.parametrize("error", [requests.HTTPError("404"), requests.Timeout("read timed out")])
def test_download_closes_response_on_http_or_stream_error(tmp_path, monkeypatch, network_config, error):
    response = StreamingResponse([b"partial"], error=error)
    install_response(monkeypatch, response)
    target = tmp_path / "download.bin"
    with pytest.raises(type(error)):
        base.urlretrieve("https://example.test/file", target)
    assert response.closed
    if isinstance(error, requests.HTTPError):
        assert not target.exists()


def test_download_closes_response_when_destination_cannot_be_opened(tmp_path, monkeypatch, network_config):
    response = StreamingResponse([b"data"])
    install_response(monkeypatch, response)
    with pytest.raises(OSError):
        base.urlretrieve("https://example.test/file", tmp_path)
    assert response.closed


def test_normal_get_keeps_non_streaming_default(monkeypatch, network_config):
    response = StreamingResponse([])
    calls = install_response(monkeypatch, response)
    assert base.Request().get("https://example.test/page") is response
    assert calls[0][1]["stream"] is False
