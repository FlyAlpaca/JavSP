"""本地 Hy-MT2 翻译与 llama.cpp 进程管理。"""

import hashlib
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from tqdm import tqdm

from javsp.config import Cfg, Translator
from javsp.datatype import MovieInfo

logger = logging.getLogger(__name__)
_JAPANESE = re.compile(r"[\u3041-\u3096\u30a1-\u30fa\u30fd-\u30fe\uff66-\uff9d]")
_MAX_CHARS = 800


class TranslateError(Exception):
    """模型准备、服务或翻译失败。"""


@dataclass(frozen=True)
class ModelSpec:
    repository: str
    revision: str
    filename: str
    size: int
    sha256: str

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repository}/resolve/{self.revision}/{self.filename}"


MODELS = {
    "hy_mt2_7b_q4_k_m": ModelSpec(
        "tencent/Hy-MT2-7B-GGUF",
        "ab8472660ac61fac25f1af43fac2599d52a8a775",
        "Hy-MT2-7B-Q4_K_M.gguf",
        4624648896,
        "9f96256500f3fc1ab4d64336b58f52a949a95ad7516b0c229476eef782f9f77b",
    ),
    "hy_mt2_1_8b_q4_k_m": ModelSpec(
        "tencent/Hy-MT2-1.8B-GGUF",
        "a0c709d9fac510f2c807aa3af52872340dc37a4a",
        "Hy-MT2-1.8B-Q4_K_M.gguf",
        1133080448,
        "dc5f44fcf1fa496ee7ad725982c0c8c553a4de00259b53af84c4b89fb0c06699",
    ),
}


def _valid_model(path: Path, spec: ModelSpec) -> bool:
    if not path.is_file() or path.stat().st_size != spec.size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == spec.sha256


def ensure_model(cfg: Translator, session=None) -> Path:
    """在启动阶段验证缓存；缺失或损坏时下载并原子替换。"""
    spec = MODELS[cfg.model]
    model_dir = cfg.model_dir
    try:
        model_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise TranslateError(f"无法创建模型缓存目录 {model_dir}: {error}") from error
    path = model_dir / spec.filename
    if _valid_model(path, spec):
        logger.info("本地翻译模型校验通过: %s", path)
        return path
    if not cfg.auto_download:
        raise TranslateError(f"模型缺失或校验失败: {path}；请放入官方 GGUF 或启用 auto_download")

    logger.info("下载本地翻译模型: %s", spec.url)
    client = session or requests.Session()
    proxy = Cfg().network.proxy_server
    request_options = {"proxies": {"http": str(proxy), "https": str(proxy)}} if proxy is not None else {}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{spec.filename}.", suffix=".part", dir=model_dir, delete=False) as file:
            temporary = Path(file.name)
            with client.get(spec.url, stream=True, timeout=(30, 120), **request_options) as response:
                response.raise_for_status()
                terminal = sys.stderr.isatty()
                downloaded = 0
                last_reported = 0
                last_report_at = time.monotonic()
                if not terminal:
                    logger.info("下载翻译模型: 0%% (%s 字节)", spec.size)
                with tqdm(
                    total=spec.size,
                    desc="下载翻译模型",
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    disable=not terminal,
                ) as progress:
                    for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                        if chunk:
                            file.write(chunk)
                            downloaded += len(chunk)
                            progress.update(len(chunk))
                            if not terminal:
                                now = time.monotonic()
                                percent = min(downloaded * 100 // spec.size, 100)
                                if percent >= last_reported + 5 or now - last_report_at >= 10:
                                    logger.info("下载翻译模型: %d%% (%d/%d 字节)", percent, downloaded, spec.size)
                                    last_reported = percent
                                    last_report_at = now
                if not terminal and downloaded * 100 // spec.size > last_reported:
                    logger.info("下载翻译模型: %d%% (%d/%d 字节)", min(downloaded * 100 // spec.size, 100), downloaded, spec.size)
        if not _valid_model(temporary, spec):
            raise TranslateError(f"模型下载校验失败: {spec.filename}")
        temporary.replace(path)
        logger.info("本地翻译模型已准备: %s", path)
        return path
    except (OSError, requests.RequestException) as error:
        raise TranslateError(
            f"模型下载失败: {error}；请检查容器到 Hugging Face 的连接或 network.proxy_server，"
            "也可手动放入官方 GGUF 并设置 translator.auto_download: false"
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if session is None:
            client.close()


class LocalTranslator:
    """单批次复用一个仅在容器内监听的 llama-server。"""

    def __init__(self, cfg: Translator, *, binary=None, port=18080, session=None):
        self.cfg = cfg
        self.binary = binary or shutil.which("llama-server")
        self.port = port
        self.session = session or requests.Session()
        self._own_session = session is None
        self.session.trust_env = False
        self.process = None

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        try:
            path = ensure_model(self.cfg)
            if not self.binary:
                raise TranslateError("找不到 llama-server；请使用包含 llama.cpp 的镜像")
            runtime = self.cfg.runtime
            command = [
                self.binary,
                "--model",
                str(path),
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--alias",
                "javsp-translate",
                "--jinja",
                "--threads",
                str(runtime.threads),
                "--threads-batch",
                str(runtime.threads_batch),
                "--ctx-size",
                str(runtime.ctx_size),
                "--batch-size",
                str(runtime.batch_size),
                "--ubatch-size",
                str(runtime.ubatch_size),
                "--parallel",
                str(runtime.parallel),
                "--n-gpu-layers",
                str(runtime.n_gpu_layers),
            ]
            self.process = subprocess.Popen(command)
            deadline = time.monotonic() + runtime.startup_timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise TranslateError(f"llama-server 提前退出，状态码 {self.process.returncode}")
                try:
                    response = self.session.get(f"{self.base_url}/health", timeout=2)
                    if response.ok and response.json().get("status") == "ok":
                        logger.info("本地翻译服务已就绪")
                        return self
                except (requests.RequestException, ValueError):
                    pass
                time.sleep(1)
            raise TranslateError("等待本地翻译服务就绪超时")
        except OSError as error:
            self.close()
            raise TranslateError(f"启动 llama-server 失败: {error}") from error
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process = None
        if self._own_session:
            self.session.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()

    def translate(self, text: str, field: str) -> str:
        prompt = _prompt(text, field)
        generation = self.cfg.generation
        payload = {
            "model": "javsp-translate",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": generation.temperature,
            "top_p": generation.top_p,
            "top_k": generation.top_k,
            "repeat_penalty": generation.repeat_penalty,
            "max_tokens": generation.max_tokens,
            "stream": False,
        }
        try:
            response = self.session.post(f"{self.base_url}/v1/chat/completions", json=payload, timeout=(10, generation.timeout))
            response.raise_for_status()
            result = response.json()
            choice = result["choices"][0]
            content = choice["message"]["content"]
            if choice.get("finish_reason") == "length" or not isinstance(content, str):
                raise TranslateError("模型输出被截断或格式无效")
            content = content.strip()
            if not content or content.startswith(
                ("```", "将以下", "请将以下", "【待翻译", "<think>", "译文：", "翻译：", "以下是")
            ):
                raise TranslateError("模型输出为空或包含提示词、解释")
            return content
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as error:
            raise TranslateError(f"本地翻译请求失败: {error}") from error


def _prompt(text: str, field: str) -> str:
    if field == "title":
        instruction = (
            "将以下日文影片标题准确翻译为简体中文。保留番号、数字、英文、人名及其他非日文内容；"
            "不增加或删减信息，不补写情节。只输出译文，不要解释："
        )
    else:
        instruction = (
            "将以下日文影片简介忠实翻译为简体中文。保留番号、数字、英文、人名及其他非日文内容；"
            "保持原意与叙述顺序，不补写、不删减、不概括。只输出译文，不要解释："
        )
    return f"{instruction}\n\n{text}"


def _chunks(text: str):
    """按段落和句末标点切分，单句过长时再按字符限制切分。"""
    for paragraph in text.splitlines(keepends=True):
        body = paragraph.rstrip("\r\n")
        suffix = paragraph[len(body) :]
        if not body:
            yield "", suffix
            continue
        current = ""
        for sentence in re.split(r"(?<=[。！？])", body):
            if not sentence:
                continue
            if current and len(current) + len(sentence) > _MAX_CHARS:
                yield current, ""
                current = ""
            while len(sentence) > _MAX_CHARS:
                yield sentence[:_MAX_CHARS], ""
                sentence = sentence[_MAX_CHARS:]
            current += sentence
        if current:
            yield current, suffix


def _translate_field(text: str, field: str, translator: LocalTranslator) -> str:
    if not _JAPANESE.search(text):
        return text
    return "".join(
        (translator.translate(chunk, field) if _JAPANESE.search(chunk) else chunk) + suffix for chunk, suffix in _chunks(text)
    )


def translate_movie_info(info: MovieInfo, translator: LocalTranslator) -> None:
    """只翻译必要字段，成功后一次性更新元数据。"""
    title = _translate_field(info.title, "title", translator) if info.title else info.title
    plot = _translate_field(info.plot, "plot", translator) if info.plot else info.plot
    if title != info.title:
        if info.ori_title is None:
            info.ori_title = info.title
        info.title = title
    if plot != info.plot:
        if info.ori_plot is None:
            info.ori_plot = info.plot
        info.plot = plot
