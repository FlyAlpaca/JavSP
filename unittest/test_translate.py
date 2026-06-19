"""翻译模块测试

使用 unittest/data 中的影片数据提取日文标题/简介作为翻译输入，
通过 mock _request 实例的方法来测试翻译逻辑的正确性。
"""

import json
import os
from glob import glob
from unittest.mock import MagicMock, patch

import pytest

from javsp.config import (
    AlibabaTranslateEngine,
    AnthropicEngine,
    BingTranslateEngine,
    GoogleTranslateEngine,
    OpenAICompatibleEngine,
)
from javsp.datatype import MovieInfo
from javsp.web.translate import (
    TranslateError,
    translate,
    translate_movie_info,
)

file_dir = os.path.dirname(__file__)
data_dir = os.path.join(file_dir, "data")


# ---------------------------------------------------------------------------
# 从测试数据中提取日文文本
# ---------------------------------------------------------------------------
def _load_test_texts():
    """从 unittest/data 的 JSON 文件中提取日文标题和简介"""
    texts = []
    seen = set()
    for filepath in glob(os.path.join(data_dir, "*.json")):
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
        title = data.get("title")
        plot = data.get("plot")
        if title and title not in seen:
            seen.add(title)
            texts.append(("title", title))
        if plot and plot not in seen:
            seen.add(plot)
            texts.append(("plot", plot))
    return texts


# 提前加载，用于参数化
_test_texts = _load_test_texts()

# 取前几条有代表性的文本用于 translate 函数测试
_sample_texts = (
    [t for t in _test_texts[:6]]
    if _test_texts
    else [
        ("title", "生意気な妹にニーハイを履かせ僕だけの「絶対領域」を誕生させ僕好みに痴女らせた。 相沢みなみ"),
        ("title", "三上悠亜と新ありなと相沢みなみ"),
    ]
)


# ---------------------------------------------------------------------------
# 测试 translate() 入口：engine=None 时直接返回原文
# ---------------------------------------------------------------------------
def test_translate_none_engine():
    result = translate("テスト", None)
    assert result == "テスト"


# ---------------------------------------------------------------------------
# 测试 translate() 入口：未知引擎类型
# ---------------------------------------------------------------------------
def test_translate_unknown_engine():
    engine = MagicMock()
    engine.type = "unknown"
    result = translate("テスト", engine)
    assert result == "テスト"


# ---------------------------------------------------------------------------
# OpenAI-compatible 引擎测试
# ---------------------------------------------------------------------------
class TestOpenAICompatible:
    @pytest.fixture
    def engine(self):
        return OpenAICompatibleEngine(
            type="openai_compatible",
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="gpt-4o-mini",
        )

    @patch("javsp.web.translate._request")
    def test_success(self, mock_req, engine):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {"message": {"content": "让傲娇的妹妹穿上过膝袜，诞生了只属于我的绝对领域，并让她按我的喜好变成了痴女。 相泽南"}}
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_req.post_json.return_value = mock_resp

        result = translate("生意気な妹にニーハイを履かせ僕だけの「絶対領域」を誕生させ僕好みに痴女らせた。 相沢みなみ", engine)
        assert "绝对领域" in result

    @patch("javsp.web.translate._request")
    def test_api_error(self, mock_req, engine):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.raise_for_status.side_effect = Exception("401 Unauthorized")
        mock_req.post_json.return_value = mock_resp

        with pytest.raises(TranslateError) as exc_info:
            translate("テスト", engine)
        assert "openai_compatible" in str(exc_info.value)

    @patch("javsp.web.translate._request")
    def test_response_error_field(self, mock_req, engine):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"error": {"message": "Invalid API key", "type": "invalid_request_error"}}
        mock_resp.raise_for_status = MagicMock()
        mock_req.post_json.return_value = mock_resp

        with pytest.raises(TranslateError) as exc_info:
            translate("テスト", engine)
        assert "openai_compatible" in str(exc_info.value)

    @patch("javsp.web.translate._request")
    def test_empty_choices(self, mock_req, engine):
        """API 返回空 choices 列表"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": []}
        mock_resp.raise_for_status = MagicMock()
        mock_req.post_json.return_value = mock_resp

        with pytest.raises(TranslateError) as exc_info:
            translate("テスト", engine)
        assert "empty choices" in str(exc_info.value)

    @patch("javsp.web.translate._request")
    def test_empty_content(self, mock_req, engine):
        """API 返回空 content"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": ""}}]}
        mock_resp.raise_for_status = MagicMock()
        mock_req.post_json.return_value = mock_resp

        with pytest.raises(TranslateError) as exc_info:
            translate("テスト", engine)
        assert "no translation returned" in str(exc_info.value)

    @patch("javsp.web.translate._request")
    def test_base_url_auto_completion(self, mock_req, engine):
        """base_url 不以 /chat/completions 结尾时自动补全"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "测试翻译"}}]}
        mock_resp.raise_for_status = MagicMock()
        mock_req.post_json.return_value = mock_resp

        translate("テスト", engine)
        called_url = mock_req.post_json.call_args[0][0]
        assert called_url.endswith("/chat/completions")

    @patch("javsp.web.translate._request")
    def test_base_url_already_complete(self, mock_req):
        """base_url 已包含 /chat/completions 时不重复拼接"""
        engine = OpenAICompatibleEngine(
            type="openai_compatible",
            base_url="https://api.openai.com/v1/chat/completions",
            api_key="test-key",
            model="gpt-4o-mini",
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "测试翻译"}}]}
        mock_resp.raise_for_status = MagicMock()
        mock_req.post_json.return_value = mock_resp

        translate("テスト", engine)
        called_url = mock_req.post_json.call_args[0][0]
        assert called_url == "https://api.openai.com/v1/chat/completions"

    @patch("javsp.web.translate._request")
    def test_custom_prompt(self, mock_req):
        engine = OpenAICompatibleEngine(
            type="openai_compatible",
            base_url="https://api.deepseek.com/v1",
            api_key="test-key",
            model="deepseek-chat",
            system_prompt="自定义提示词",
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "翻译结果"}}]}
        mock_resp.raise_for_status = MagicMock()
        mock_req.post_json.return_value = mock_resp

        translate("テスト", engine)
        call_data = mock_req.post_json.call_args[1]["json_data"]
        assert call_data["messages"][0]["content"] == "自定义提示词"


# ---------------------------------------------------------------------------
# Anthropic 引擎测试
# ---------------------------------------------------------------------------
class TestAnthropic:
    @pytest.fixture
    def engine(self):
        return AnthropicEngine(
            type="anthropic",
            api_key="test-key",
            model="claude-3-5-sonnet-20241022",
        )

    @patch("javsp.web.translate._request")
    def test_success(self, mock_req, engine):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"content": [{"text": "三上悠亚与新有菜和相泽南"}]}
        mock_resp.raise_for_status = MagicMock()
        mock_req.post_json.return_value = mock_resp

        result = translate("三上悠亜と新ありなと相沢みなみ", engine)
        assert "三上" in result

    @patch("javsp.web.translate._request")
    def test_api_error(self, mock_req, engine):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.raise_for_status.side_effect = Exception("403 Forbidden")
        mock_req.post_json.return_value = mock_resp

        with pytest.raises(TranslateError) as exc_info:
            translate("テスト", engine)
        assert "anthropic" in str(exc_info.value)

    @patch("javsp.web.translate._request")
    def test_empty_content(self, mock_req, engine):
        """API 返回空 content 列表"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"content": []}
        mock_resp.raise_for_status = MagicMock()
        mock_req.post_json.return_value = mock_resp

        with pytest.raises(TranslateError) as exc_info:
            translate("テスト", engine)
        assert "empty content" in str(exc_info.value)

    @patch("javsp.web.translate._request")
    def test_base_url_auto_completion(self, mock_req, engine):
        """base_url 不以 /messages 结尾时自动补全"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"content": [{"text": "翻译结果"}]}
        mock_resp.raise_for_status = MagicMock()
        mock_req.post_json.return_value = mock_resp

        translate("テスト", engine)
        called_url = mock_req.post_json.call_args[0][0]
        assert called_url.endswith("/messages")

    @patch("javsp.web.translate._request")
    def test_custom_base_url(self, mock_req):
        """自定义 base_url（如中转代理）"""
        engine = AnthropicEngine(
            type="anthropic",
            base_url="https://my-proxy.example.com/anthropic",
            api_key="test-key",
            model="claude-3-5-sonnet-20241022",
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"content": [{"text": "翻译结果"}]}
        mock_resp.raise_for_status = MagicMock()
        mock_req.post_json.return_value = mock_resp

        translate("テスト", engine)
        called_url = mock_req.post_json.call_args[0][0]
        assert called_url == "https://my-proxy.example.com/anthropic/messages"


# ---------------------------------------------------------------------------
# Google 翻译引擎测试
# ---------------------------------------------------------------------------
class TestGoogleTranslate:
    @pytest.fixture
    def engine(self):
        return GoogleTranslateEngine(type="google")

    @patch("javsp.web.translate._request")
    def test_success(self, mock_req, engine):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            [["让傲娇的妹妹穿上过膝袜", "生意気な妹にニーハイを履かせ", None, None, 10]],
            None,
            "ja",
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_req.post.return_value = mock_resp

        result = translate("生意気な妹にニーハイを履かせ", engine)
        assert "过膝袜" in result

    @patch("javsp.web.translate._request")
    def test_empty_result(self, mock_req, engine):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_req.post.return_value = mock_resp

        with pytest.raises(TranslateError) as exc_info:
            translate("テスト", engine)
        assert "no translation returned" in str(exc_info.value)

    @patch("javsp.web.translate._request")
    def test_exception(self, mock_req, engine):
        mock_req.post.side_effect = Exception("Connection error")
        with pytest.raises(TranslateError) as exc_info:
            translate("テスト", engine)
        assert "google" in str(exc_info.value)
        assert "Connection error" in str(exc_info.value)

    @patch("javsp.web.translate._request")
    def test_custom_language(self, mock_req):
        engine = GoogleTranslateEngine(type="google", source_language="ja", target_language="en")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [[["test translation", "テスト", None, None, 10]]]
        mock_resp.raise_for_status = MagicMock()
        mock_req.post.return_value = mock_resp

        result = translate("テスト", engine)
        assert "test" in result.lower()
        # 验证 URL 中包含自定义语言参数
        called_url = mock_req.post.call_args[0][0]
        assert "sl=ja" in called_url
        assert "tl=en" in called_url


# ---------------------------------------------------------------------------
# Bing 翻译引擎测试
# ---------------------------------------------------------------------------
class TestBingTranslate:
    @pytest.fixture
    def engine(self):
        return BingTranslateEngine(type="bing")

    @patch("javsp.web.translate._request")
    def test_success(self, mock_req, engine):
        # mock token 请求
        mock_token_resp = MagicMock()
        mock_token_resp.status_code = 200
        mock_token_resp.text = "fake_jwt_token"
        mock_token_resp.raise_for_status = MagicMock()

        # mock 翻译请求
        mock_trans_resp = MagicMock()
        mock_trans_resp.status_code = 200
        mock_trans_resp.json.return_value = [{"translations": [{"text": "测试翻译", "to": "zh-Hans"}]}]
        mock_trans_resp.raise_for_status = MagicMock()

        mock_req.get.return_value = mock_token_resp
        mock_req.post_json.return_value = mock_trans_resp

        result = translate("テスト", engine)
        assert result == "测试翻译"

    @patch("javsp.web.translate._request")
    def test_token_error(self, mock_req, engine):
        """获取 token 失败"""
        mock_req.get.side_effect = Exception("Token request failed")
        with pytest.raises(TranslateError) as exc_info:
            translate("テスト", engine)
        assert "bing" in str(exc_info.value)

    @patch("javsp.web.translate._request")
    def test_empty_result(self, mock_req, engine):
        mock_token_resp = MagicMock()
        mock_token_resp.status_code = 200
        mock_token_resp.text = "fake_jwt_token"
        mock_token_resp.raise_for_status = MagicMock()

        mock_trans_resp = MagicMock()
        mock_trans_resp.status_code = 200
        mock_trans_resp.json.return_value = []
        mock_trans_resp.raise_for_status = MagicMock()

        mock_req.get.return_value = mock_token_resp
        mock_req.post_json.return_value = mock_trans_resp

        with pytest.raises(TranslateError) as exc_info:
            translate("テスト", engine)
        assert "no translation returned" in str(exc_info.value)

    @patch("javsp.web.translate._request")
    def test_custom_language(self, mock_req):
        engine = BingTranslateEngine(type="bing", source_language="ja", target_language="en")
        mock_token_resp = MagicMock()
        mock_token_resp.status_code = 200
        mock_token_resp.text = "fake_jwt_token"
        mock_token_resp.raise_for_status = MagicMock()

        mock_trans_resp = MagicMock()
        mock_trans_resp.status_code = 200
        mock_trans_resp.json.return_value = [{"translations": [{"text": "test", "to": "en"}]}]
        mock_trans_resp.raise_for_status = MagicMock()

        mock_req.get.return_value = mock_token_resp
        mock_req.post_json.return_value = mock_trans_resp

        result = translate("テスト", engine)
        assert result == "test"
        # 验证 URL 中包含自定义语言参数
        called_url = mock_req.post_json.call_args[0][0]
        assert "from=ja" in called_url
        assert "to=en" in called_url

    @patch("javsp.web.translate._request")
    def test_auto_language_no_from_param(self, mock_req):
        """source_language=auto 时不传 from 参数，让 Bing 自动检测源语言"""
        engine = BingTranslateEngine(type="bing")  # 默认 source_language="auto"
        mock_token_resp = MagicMock()
        mock_token_resp.status_code = 200
        mock_token_resp.text = "fake_jwt_token"
        mock_token_resp.raise_for_status = MagicMock()

        mock_trans_resp = MagicMock()
        mock_trans_resp.status_code = 200
        mock_trans_resp.json.return_value = [{"translations": [{"text": "测试翻译", "to": "zh-Hans"}]}]
        mock_trans_resp.raise_for_status = MagicMock()

        mock_req.get.return_value = mock_token_resp
        mock_req.post_json.return_value = mock_trans_resp

        result = translate("テスト", engine)
        assert result == "测试翻译"
        # auto 时不传 from 参数
        called_url = mock_req.post_json.call_args[0][0]
        assert "from=" not in called_url
        assert "to=zh-Hans" in called_url


# ---------------------------------------------------------------------------
# 阿里翻译引擎测试
# ---------------------------------------------------------------------------
class TestAlibabaTranslate:
    @pytest.fixture
    def engine(self):
        return AlibabaTranslateEngine(type="alibaba")

    @patch("javsp.web.translate._request")
    def test_success(self, mock_req, engine):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"translateText": "测试翻译"}}
        mock_resp.raise_for_status = MagicMock()
        mock_req.get.return_value = mock_resp

        result = translate("テスト", engine)
        assert result == "测试翻译"

    @patch("javsp.web.translate._request")
    def test_html_stripping(self, mock_req, engine):
        """阿里翻译返回带 HTML 标签的结果时应清理"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"translateText": "测试<b>翻译</b>"}}
        mock_resp.raise_for_status = MagicMock()
        mock_req.get.return_value = mock_resp

        result = translate("テスト", engine)
        assert "<b>" not in result
        assert result == "测试翻译"

    @patch("javsp.web.translate._request")
    def test_empty_result(self, mock_req, engine):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"translateText": ""}}
        mock_resp.raise_for_status = MagicMock()
        mock_req.get.return_value = mock_resp

        with pytest.raises(TranslateError) as exc_info:
            translate("テスト", engine)
        assert "no translation returned" in str(exc_info.value)

    @patch("javsp.web.translate._request")
    def test_exception(self, mock_req, engine):
        mock_req.get.side_effect = Exception("Connection error")
        with pytest.raises(TranslateError) as exc_info:
            translate("テスト", engine)
        assert "alibaba" in str(exc_info.value)

    @patch("javsp.web.translate._request")
    def test_custom_language(self, mock_req):
        engine = AlibabaTranslateEngine(type="alibaba", source_language="ja", target_language="en")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"translateText": "test translation"}}
        mock_resp.raise_for_status = MagicMock()
        mock_req.get.return_value = mock_resp

        result = translate("テスト", engine)
        assert result == "test translation"
        # 验证 URL 中包含自定义语言参数
        called_url = mock_req.get.call_args[0][0]
        assert "srcLang=ja" in called_url
        assert "tgtLang=en" in called_url


# ---------------------------------------------------------------------------
# translate_movie_info 测试
# ---------------------------------------------------------------------------
class TestTranslateMovieInfo:
    @pytest.fixture
    def movie_info(self):
        info = MovieInfo("ABP-001")
        info.title = "テストタイトル"
        info.plot = "テストプロット"
        return info

    @patch("javsp.web.translate._request")
    def test_translate_title_and_plot(self, mock_req, movie_info):
        """同时翻译标题和简介"""
        engine = OpenAICompatibleEngine(
            type="openai_compatible",
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="gpt-4o-mini",
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_req.post_json.return_value = mock_resp

        # 第一次调用翻译标题，第二次翻译简介
        mock_resp.json.side_effect = [
            {"choices": [{"message": {"content": "测试标题"}}]},
            {"choices": [{"message": {"content": "测试简介"}}]},
        ]

        mock_cfg = MagicMock()
        mock_cfg.translator.engine = engine
        mock_cfg.translator.fields.title = True
        mock_cfg.translator.fields.plot = True
        with patch("javsp.web.translate.Cfg", return_value=mock_cfg):
            translate_movie_info(movie_info)

        assert movie_info.ori_title == "テストタイトル"
        assert movie_info.title == "测试标题"
        assert movie_info.ori_plot == "テストプロット"
        assert movie_info.plot == "测试简介"

    def test_no_engine(self, movie_info):
        """未配置翻译引擎时不翻译"""
        mock_cfg = MagicMock()
        mock_cfg.translator.engine = None
        mock_cfg.translator.fields.title = True
        mock_cfg.translator.fields.plot = True
        with patch("javsp.web.translate.Cfg", return_value=mock_cfg):
            result = translate_movie_info(movie_info)
        assert result is True
        assert movie_info.title == "テストタイトル"

    @patch("javsp.web.translate._request")
    def test_translate_error_raises(self, mock_req, movie_info):
        """翻译失败时抛出异常"""
        engine = OpenAICompatibleEngine(
            type="openai_compatible",
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="gpt-4o-mini",
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.raise_for_status.side_effect = Exception("401 Unauthorized")
        mock_req.post_json.return_value = mock_resp

        mock_cfg = MagicMock()
        mock_cfg.translator.engine = engine
        mock_cfg.translator.fields.title = True
        mock_cfg.translator.fields.plot = True
        with patch("javsp.web.translate.Cfg", return_value=mock_cfg):
            with pytest.raises(Exception):
                translate_movie_info(movie_info)

    @patch("javsp.web.translate._request")
    def test_translate_title_fails_plot_succeeds(self, mock_req, movie_info):
        """标题翻译失败但简介翻译成功时，仍抛出异常"""
        engine = OpenAICompatibleEngine(
            type="openai_compatible",
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="gpt-4o-mini",
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_req.post_json.return_value = mock_resp

        # 第一次（标题）失败，第二次（简介）成功
        mock_resp.json.side_effect = [
            {"choices": []},  # empty choices → TranslateError
            {"choices": [{"message": {"content": "测试简介"}}]},
        ]

        mock_cfg = MagicMock()
        mock_cfg.translator.engine = engine
        mock_cfg.translator.fields.title = True
        mock_cfg.translator.fields.plot = True
        with patch("javsp.web.translate.Cfg", return_value=mock_cfg):
            with pytest.raises(Exception) as exc_info:
                translate_movie_info(movie_info)
            # 简介应该已经翻译成功
            assert movie_info.plot == "测试简介"
            # 但标题翻译失败导致整体抛异常
            assert "翻译标题时出错" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 参数化测试：使用真实测试数据
# ---------------------------------------------------------------------------
class TestParametrized:
    """使用 unittest/data 中的真实数据测试各引擎"""

    @pytest.fixture
    def openai_engine(self):
        return OpenAICompatibleEngine(
            type="openai_compatible",
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="gpt-4o-mini",
        )

    @patch("javsp.web.translate._request")
    @pytest.mark.parametrize("field,text", _sample_texts)
    def test_openai_with_real_data(self, mock_req, openai_engine, field, text):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "翻译结果"}}]}
        mock_resp.raise_for_status = MagicMock()
        mock_req.post_json.return_value = mock_resp

        result = translate(text, openai_engine)
        assert result == "翻译结果"

    @pytest.fixture
    def google_engine(self):
        return GoogleTranslateEngine(type="google")

    @patch("javsp.web.translate._request")
    @pytest.mark.parametrize("field,text", _sample_texts)
    def test_google_with_real_data(self, mock_req, google_engine, field, text):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [[["翻译结果", text, None, None, 10]]]
        mock_resp.raise_for_status = MagicMock()
        mock_req.post.return_value = mock_resp

        result = translate(text, google_engine)
        assert result == "翻译结果"
