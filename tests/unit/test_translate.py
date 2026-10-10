"""本地模型准备、请求和元数据翻译的离线测试。"""

import hashlib
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from javsp.config import Cfg, Translator
from javsp.datatype import MovieInfo
from javsp.translate import (
    MODELS,
    LocalTranslator,
    ModelSpec,
    TranslateError,
    _prompt,
    ensure_model,
    translate_movie_info,
)


def test_default_and_removed_config():
    cfg = Cfg()
    assert cfg.translator.model == "hy_mt2_7b_q4_k_m"
    assert cfg.translator.runtime.ctx_size == 8192
    assert cfg.translator.generation.top_p == 0.6
    assert Translator(model="hy_mt2_1_8b_q4_k_m").model == "hy_mt2_1_8b_q4_k_m"
    for removed in ({"engine": None}, {"engine": {"type": "bing"}}, {"fields": {"title": False}}):
        with pytest.raises(ValueError, match="旧翻译配置已移除"):
            Translator.model_validate(removed)


@pytest.fixture
def small_model(monkeypatch):
    content = b"GGUF-test-model" * 100
    spec = ModelSpec("tencent/test", "revision", "test.gguf", len(content), hashlib.sha256(content).hexdigest())
    monkeypatch.setitem(MODELS, "hy_mt2_7b_q4_k_m", spec)
    return spec, content


def test_cached_model_needs_no_network(tmp_path, small_model):
    spec, content = small_model
    path = tmp_path / spec.filename
    path.write_bytes(content)
    session = MagicMock()
    assert ensure_model(Translator(model_dir=tmp_path), session) == path
    session.get.assert_not_called()


@pytest.fixture
def model_server(small_model, monkeypatch):
    spec, content = small_model
    state = SimpleNamespace(
        requests=[], ignore_range=False, probe_ignored=False, bad_range=False, fail_once=False, corrupted=False
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested = self.headers.get("Range")
            state.requests.append(requested)
            data = b"X" * len(content) if state.corrupted else content
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", requested or "")
            if match and not state.ignore_range and not (state.probe_ignored and requested == "bytes=0-0"):
                start, end = map(int, match.groups())
                if state.bad_range and requested != "bytes=0-0":
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes 0-{end}/{len(data)}")
                    self.end_headers()
                    return
                chunk = data[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            else:
                chunk = data
                self.send_response(200)
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            if state.fail_once and requested != "bytes=0-0":
                state.fail_once = False
                self.wfile.write(chunk[: len(chunk) // 2])
                self.wfile.flush()
                self.close_connection = True
            else:
                self.wfile.write(chunk)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setattr(ModelSpec, "url", property(lambda self: f"http://127.0.0.1:{server.server_port}/model"))
    yield state
    server.shutdown()
    thread.join()
    server.server_close()


def test_download_replaces_invalid_cache_atomically(tmp_path, small_model, model_server, caplog):
    caplog.set_level("INFO", logger="javsp.translate")
    spec, content = small_model
    path = tmp_path / spec.filename
    path.write_bytes(b"damaged")
    assert ensure_model(Translator(model_dir=tmp_path)) == path
    assert path.read_bytes() == content
    assert list(tmp_path.glob("*.part")) == []
    assert len([item for item in model_server.requests if item != "bytes=0-0"]) == 4
    assert "下载翻译模型: 0%" in caplog.text
    assert "下载翻译模型: 100%" in caplog.text


def test_model_download_uses_configured_proxy(tmp_path, small_model, monkeypatch):
    _, content = small_model
    response = MagicMock(status_code=200, headers={})
    response.iter_content.return_value = [content]
    session = MagicMock()
    session.get.return_value.__enter__.return_value = response
    network = SimpleNamespace(proxy_server="http://proxy:7890")
    monkeypatch.setattr("javsp.translate.Cfg", lambda: SimpleNamespace(network=network))

    ensure_model(Translator(model_dir=tmp_path), session)

    assert session.get.call_args_list[0].kwargs["proxies"] == {
        "http": "http://proxy:7890",
        "https": "http://proxy:7890",
    }


def test_segment_connections_receive_proxy(tmp_path, small_model, model_server, monkeypatch):
    import requests.sessions

    network = SimpleNamespace(proxy_server="http://proxy:7890")
    monkeypatch.setattr("javsp.translate.Cfg", lambda: SimpleNamespace(network=network))
    original_get = requests.sessions.Session.get
    seen = []

    def get_without_proxy(client, url, **kwargs):
        seen.append(kwargs.pop("proxies", None))
        return original_get(client, url, **kwargs)

    monkeypatch.setattr(requests.sessions.Session, "get", get_without_proxy)
    ensure_model(Translator(model_dir=tmp_path))
    assert len(seen) == 5  # 范围探测和四个分片
    assert all(item == {"http": "http://proxy:7890", "https": "http://proxy:7890"} for item in seen)


def test_failed_download_preserves_existing_file(tmp_path, small_model):
    spec, _ = small_model
    path = tmp_path / spec.filename
    path.write_bytes(b"old")
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("offline")
    with pytest.raises(TranslateError, match="模型下载失败"):
        ensure_model(Translator(model_dir=tmp_path), session)
    assert path.read_bytes() == b"old"
    assert list(tmp_path.glob("*.part")) == []


def test_resume_partial_segment(tmp_path, small_model, model_server, monkeypatch):
    spec, content = small_model
    monkeypatch.setattr("javsp.download.CHUNK_SIZE", 32)
    model_server.fail_once = True
    with pytest.raises(TranslateError, match="模型下载失败"):
        ensure_model(Translator(model_dir=tmp_path))
    parts = list(tmp_path.glob("*.part*"))
    assert parts and any(part.stat().st_size for part in parts)
    previous = len(model_server.requests)
    assert ensure_model(Translator(model_dir=tmp_path)).read_bytes() == content
    segment_start = {i * spec.size // 4 for i in range(4)}
    assert any(
        request and int(request.removeprefix("bytes=").split("-")[0]) not in segment_start
        for request in model_server.requests[previous:]
        if request != "bytes=0-0"
    )
    assert not list(tmp_path.glob("*.part*"))


@pytest.mark.parametrize("mode", ["ignore_range", "bad_range"])
def test_range_fallback(tmp_path, small_model, model_server, mode):
    _, content = small_model
    setattr(model_server, mode, True)
    assert ensure_model(Translator(model_dir=tmp_path)).read_bytes() == content
    assert None in model_server.requests


def test_single_connection_resume(tmp_path, small_model, model_server, monkeypatch):
    _, content = small_model
    monkeypatch.setattr("javsp.download.CHUNK_SIZE", 32)
    model_server.probe_ignored = True
    model_server.fail_once = True
    with pytest.raises(TranslateError, match="模型下载失败"):
        ensure_model(Translator(model_dir=tmp_path))
    previous = len(model_server.requests)
    assert ensure_model(Translator(model_dir=tmp_path)).read_bytes() == content
    assert any(request and request != "bytes=0-0" for request in model_server.requests[previous:])


def test_checksum_failure_preserves_existing_cache(tmp_path, small_model, model_server):
    spec, _ = small_model
    old = tmp_path / spec.filename
    old.write_bytes(b"old")
    model_server.corrupted = True
    with pytest.raises(TranslateError, match="校验失败"):
        ensure_model(Translator(model_dir=tmp_path))
    assert old.read_bytes() == b"old"
    assert not list(tmp_path.glob("*.part*"))


def test_oversized_segment_is_discarded(tmp_path, small_model, model_server):
    from javsp.translate import _parts

    spec, content = small_model
    start, end, part = _parts(spec, tmp_path)[0]
    part.write_bytes(b"X" * (end - start + 2))
    assert ensure_model(Translator(model_dir=tmp_path)).read_bytes() == content
    assert f"bytes={start}-{end}" in model_server.requests


def test_terminal_progress_uses_single_tqdm_bar(monkeypatch):
    import logging

    from javsp.download import DownloadProgress

    bar = MagicMock()
    constructor = MagicMock(return_value=bar)
    monkeypatch.setattr("javsp.download.tqdm", constructor)
    monkeypatch.setattr("javsp.download.sys.stderr.isatty", lambda: True)
    with DownloadProgress(100, 25, "下载翻译模型", logging.getLogger("javsp.translate")) as progress:
        progress.update(20)
    assert constructor.call_args.kwargs["initial"] == 25
    assert constructor.call_args.kwargs["disable"] is False
    bar.update.assert_called_once_with(20)
    bar.close.assert_called_once()


def test_missing_model_offline_fails(tmp_path, small_model):
    with pytest.raises(TranslateError, match="模型缺失或校验失败"):
        ensure_model(Translator(model_dir=tmp_path, auto_download=False))


def test_runtime_starts_local_server_and_closes(tmp_path, small_model, monkeypatch):
    spec, content = small_model
    (tmp_path / spec.filename).write_bytes(content)
    process = MagicMock()
    process.poll.return_value = None
    monkeypatch.setattr("javsp.translate.subprocess.Popen", lambda command: process)
    session = MagicMock()
    session.get.return_value.ok = True
    session.get.return_value.json.return_value = {"status": "ok"}
    with LocalTranslator(Translator(model_dir=tmp_path), binary="llama-server", session=session) as runtime:
        assert runtime.base_url == "http://127.0.0.1:18080"
    process.terminate.assert_called_once()
    process.wait.assert_called_once()


def test_japanese_only_and_atomic_metadata_update():
    translator = SimpleNamespace(translate=MagicMock(side_effect=["中文标题", "中文简介"]))
    info = MovieInfo("ABC-123", title="日本語タイトル ABC-123", plot="日本語の紹介。")
    translate_movie_info(info, translator)
    assert (info.title, info.ori_title) == ("中文标题", "日本語タイトル ABC-123")
    assert (info.plot, info.ori_plot) == ("中文简介", "日本語の紹介。")
    assert [call.args[1] for call in translator.translate.call_args_list] == ["title", "plot"]

    translator.translate.reset_mock()
    info = MovieInfo("ABC-124", title="中文・标题", plot="English plot")
    translate_movie_info(info, translator)
    translator.translate.assert_not_called()
    assert info.ori_title is None and info.ori_plot is None


def test_translation_error_does_not_partially_update():
    translator = SimpleNamespace(translate=MagicMock(side_effect=["中文标题", TranslateError("timeout")]))
    info = MovieInfo("ABC-123", title="日本語タイトル", plot="日本語の紹介")
    with pytest.raises(TranslateError):
        translate_movie_info(info, translator)
    assert info.title == "日本語タイトル" and info.ori_title is None


def test_long_plot_is_chunked_and_keeps_order():
    translator = SimpleNamespace(translate=MagicMock(side_effect=lambda text, _: text.replace("あ", "中")))
    info = MovieInfo("ABC-123", plot="あ。" * 500)
    translate_movie_info(info, translator)
    assert info.plot == "中。" * 500
    assert translator.translate.call_count == 2


def test_field_prompts_and_invalid_output():
    assert "影片标题" in _prompt("日本語", "title")
    assert "不补写" in _prompt("日本語", "plot")
    session = MagicMock()
    session.post.return_value.json.return_value = {"choices": [{"message": {"content": "译文"}, "finish_reason": "stop"}]}
    runtime = LocalTranslator(Translator(), binary="llama-server", session=session)
    assert runtime.translate("日本語", "title") == "译文"
    payload = session.post.call_args.kwargs["json"]
    assert payload["messages"] == [{"role": "user", "content": _prompt("日本語", "title")}]
    assert payload["top_k"] == 20 and payload["repeat_penalty"] == 1.05
    session.post.return_value.json.return_value["choices"][0]["finish_reason"] = "length"
    with pytest.raises(TranslateError, match="截断"):
        runtime.translate("日本語", "title")
