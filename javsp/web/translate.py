"""网页翻译接口

各引擎函数成功返回 str，失败抛出 TranslateError。
translate() 作为统一入口，捕获异常并返回结果。
"""

import logging
import re

__all__ = ["translate", "translate_movie_info", "TranslateError"]

from javsp.config import (
    AlibabaTranslateEngine,
    AnthropicEngine,
    BingTranslateEngine,
    Cfg,
    GoogleTranslateEngine,
    OpenAICompatibleEngine,
)
from javsp.datatype import MovieInfo
from javsp.web.base import Request

logger = logging.getLogger(__name__)

# 模块级 Request 实例，自动继承代理/超时/UA 配置
_request = Request()


class TranslateError(Exception):
    """翻译失败异常"""

    def __init__(self, engine: str, message: str):
        self.engine = engine
        super().__init__(f"{engine}: {message}")


def translate_movie_info(info: MovieInfo):
    """根据配置翻译影片信息"""
    engine = Cfg().translator.engine
    if engine is None:
        return True

    errors = []

    # 翻译标题
    if info.title and Cfg().translator.fields.title and info.ori_title is None:
        try:
            translated = translate(info.title, engine)
            info.ori_title = info.title
            info.title = translated
        except TranslateError as e:
            errors.append(f"翻译标题时出错: {e}")

    # 翻译简介
    if info.plot and Cfg().translator.fields.plot:
        try:
            translated = translate(info.plot, engine)
            info.ori_plot = info.plot
            info.plot = translated
        except TranslateError as e:
            errors.append(f"翻译简介时出错: {e}")

    if errors:
        for e in errors:
            logger.error(e)
        raise Exception("; ".join(errors))
    return True


def translate(texts, engine) -> str:
    """翻译入口

    Args:
        texts: 待翻译文本
        engine: 翻译引擎配置

    Returns:
        str: 翻译结果

    Raises:
        TranslateError: 翻译失败
    """
    if engine is None:
        return texts

    engine_map = {
        "openai_compatible": openai_compatible_translate,
        "anthropic": anthropic_translate,
        "google": google_translate,
        "bing": bing_translate,
        "alibaba": alibaba_translate,
    }

    handler = engine_map.get(engine.type)
    if handler is None:
        return texts

    return handler(texts, engine)


# =============================================================================
# Google Translate (免费，无需 API Key)
# =============================================================================
def google_translate(texts, engine: GoogleTranslateEngine) -> str:
    """使用 Google Translate 非官方 API 进行翻译"""
    url = "https://translate.google.com/translate_a/single"
    params = {
        "client": "gtx",
        "sl": engine.source_language,
        "tl": engine.target_language,
        "dt": "t",
        "q": texts,
    }
    try:
        import urllib.parse

        full_url = url + "?" + urllib.parse.urlencode(params)
        r = _request.get(full_url, delay_raise=True)
        r.raise_for_status()
        result = r.json()
        translated_parts = []
        if result and isinstance(result, list):
            for sentence in result[0]:
                if isinstance(sentence, list) and sentence:
                    translated_parts.append(sentence[0])
        translated = "".join(translated_parts)
        if translated:
            return translated
        raise TranslateError("google", "no translation returned")
    except TranslateError:
        raise
    except Exception as e:
        raise TranslateError("google", str(e)) from e


# =============================================================================
# Bing Translate (免费，无需 API Key)
# =============================================================================
def bing_translate(texts, engine: BingTranslateEngine) -> str:
    """使用 Bing Translator API 进行翻译"""
    try:
        # 第一步：获取 token
        token_url = "https://edge.microsoft.com/translate/auth"
        token_r = _request.get(token_url, delay_raise=True)
        token_r.raise_for_status()
        jwt_token = token_r.text

        # 第二步：调用翻译 API
        api_url = "https://api.cognitive.microsofttranslator.com/translate"
        params = {
            "to": engine.target_language,
            "api-version": "3.0",
        }
        if engine.source_language and engine.source_language != "auto":
            params["from"] = engine.source_language
        saved_headers = _request.headers.copy()
        _request.headers["Authorization"] = f"Bearer {jwt_token}"
        _request.headers["Content-Type"] = "application/json"
        try:
            import urllib.parse

            full_url = api_url + "?" + urllib.parse.urlencode(params)
            r = _request.post_json(full_url, json_data=[{"text": texts}], delay_raise=True)
        finally:
            _request.headers = saved_headers
        r.raise_for_status()
        result = r.json()
        if result and isinstance(result, list) and len(result) > 0:
            translated = result[0].get("translations", [{}])[0].get("text", "")
            if translated:
                return translated
        raise TranslateError("bing", "no translation returned")
    except TranslateError:
        raise
    except Exception as e:
        raise TranslateError("bing", str(e)) from e


# =============================================================================
# Alibaba Translate (免费，无需 API Key)
# =============================================================================
def alibaba_translate(texts, engine: AlibabaTranslateEngine) -> str:
    """使用阿里翻译 Web 接口进行翻译"""
    url = "https://translate.alibaba.com/api/translate/text"
    params = {
        "srcLang": engine.source_language,
        "tgtLang": engine.target_language,
        "domain": "general",
        "query": texts,
    }
    try:
        import urllib.parse

        full_url = url + "?" + urllib.parse.urlencode(params)
        saved_headers = _request.headers.copy()
        _request.headers["Referer"] = "https://translate.alibaba.com/"
        try:
            r = _request.get(full_url, delay_raise=True)
        finally:
            _request.headers = saved_headers
        r.raise_for_status()
        result = r.json()
        translated = result.get("data", {}).get("translateText", "")
        if translated:
            translated = re.sub(r"<[^>]+>", "", translated)
            return translated
        raise TranslateError("alibaba", "no translation returned")
    except TranslateError:
        raise
    except Exception as e:
        raise TranslateError("alibaba", str(e)) from e


# =============================================================================
# OpenAI-compatible
# =============================================================================
def openai_compatible_translate(texts, engine: OpenAICompatibleEngine) -> str:
    api_url = str(engine.base_url)
    if not api_url.endswith("/chat/completions"):
        api_url = api_url.rstrip("/") + "/chat/completions"

    data = {
        "model": engine.model,
        "messages": [
            {"role": "system", "content": engine.system_prompt},
            {"role": "user", "content": texts},
        ],
        "temperature": engine.temperature,
        "max_tokens": engine.max_tokens,
    }

    try:
        saved_headers = _request.headers.copy()
        _request.headers["Content-Type"] = "application/json"
        _request.headers["Authorization"] = f"Bearer {engine.api_key}"
        try:
            r = _request.post_json(api_url, json_data=data, delay_raise=True)
        finally:
            _request.headers = saved_headers
        r.raise_for_status()
        resp = r.json()
        if "error" in resp:
            raise TranslateError("openai_compatible", str(resp["error"]))
        choices = resp.get("choices", [])
        if not choices:
            raise TranslateError("openai_compatible", "empty choices in response")
        content = choices[0].get("message", {}).get("content", "").strip()
        if not content:
            raise TranslateError("openai_compatible", "no translation returned")
        return content
    except TranslateError:
        raise
    except Exception as e:
        raise TranslateError("openai_compatible", str(e)) from e


# =============================================================================
# Anthropic
# =============================================================================
def anthropic_translate(texts, engine: AnthropicEngine) -> str:
    api_url = str(engine.base_url)
    if not api_url.endswith("/messages"):
        api_url = api_url.rstrip("/") + "/messages"

    data = {
        "model": engine.model,
        "max_tokens": engine.max_tokens,
        "temperature": engine.temperature,
        "system": engine.system_prompt,
        "messages": [{"role": "user", "content": texts}],
    }

    try:
        saved_headers = _request.headers.copy()
        _request.headers["x-api-key"] = engine.api_key
        _request.headers["content-type"] = "application/json"
        _request.headers["anthropic-version"] = "2023-06-01"
        try:
            r = _request.post_json(api_url, json_data=data, delay_raise=True)
        finally:
            _request.headers = saved_headers
        r.raise_for_status()
        resp = r.json()
        content_list = resp.get("content", [])
        if not content_list:
            raise TranslateError("anthropic", "empty content in response")
        content = content_list[0].get("text", "").strip()
        if not content:
            raise TranslateError("anthropic", "no translation returned")
        return content
    except TranslateError:
        raise
    except Exception as e:
        raise TranslateError("anthropic", str(e)) from e
