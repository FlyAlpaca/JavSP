"""本地模型准备、请求和元数据翻译的离线测试。"""

import hashlib
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
    content = b"GGUF-test-model"
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


def test_download_replaces_invalid_cache_atomically(tmp_path, small_model, capsys):
    spec, content = small_model
    path = tmp_path / spec.filename
    path.write_bytes(b"damaged")
    response = MagicMock()
    response.iter_content.return_value = [content[:4], content[4:]]
    session = MagicMock()
    session.get.return_value.__enter__.return_value = response
    assert ensure_model(Translator(model_dir=tmp_path), session) == path
    assert path.read_bytes() == content
    assert list(tmp_path.glob("*.part")) == []
    assert session.get.call_args.args[0] == spec.url
    output = capsys.readouterr().err
    assert "下载翻译模型" in output
    assert "100%" in output


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
