import logging
import os
import sys
from argparse import ArgumentParser, RawTextHelpFormatter
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from confz import BaseConfig, DataSource, EnvSource, FileSource
from pydantic import ByteSize, Field, NonNegativeInt, PositiveInt, model_validator
from pydantic_core import Url
from pydantic_extra_types.pendulum_dt import Duration

from javsp.lib import resource_path

_logger = logging.getLogger(__name__)

DEFAULT_CONFIG_FILE = "config_default.yml"
USER_CONFIG_FILE = "config/config.yml"


class ConfigLoadError(ValueError):
    """用户配置无法读取或解析。"""


def _user_config_source(path):
    try:
        with open(path, encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ConfigLoadError(f"无法读取或解析配置文件: {path}") from error
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ConfigLoadError(f"配置文件顶层必须是映射: {path}")
    return FileSource(file=path)


class Concurrency(BaseConfig):
    movies: PositiveInt = 3
    images: PositiveInt = 3


class Scanner(BaseConfig):
    ignored_id_pattern: list[str]
    input_directory: Path | None = None
    filename_extensions: list[str]
    ignored_folder_name_pattern: list[str]
    minimum_size: ByteSize
    skip_nfo_dir: bool


class Network(BaseConfig):
    proxy_server: Url | None
    retry: NonNegativeInt = 3
    timeout: Duration


class MovieInfoField(StrEnum):
    dvdid = "dvdid"
    cid = "cid"
    url = "url"
    plot = "plot"
    cover = "cover"
    big_cover = "big_cover"
    genre = "genre"
    genre_id = "genre_id"
    genre_norm = "genre_norm"
    score = "score"
    title = "title"
    ori_title = "ori_title"
    magnet = "magnet"
    serial = "serial"
    actress = "actress"
    actress_pics = "actress_pics"
    director = "director"
    duration = "duration"
    producer = "producer"
    publisher = "publisher"
    uncensored = "uncensored"
    publish_date = "publish_date"
    preview_pics = "preview_pics"
    preview_video = "preview_video"


class R18Dev(BaseConfig):
    required_keys: list[MovieInfoField] = Field(default_factory=lambda: [MovieInfoField.cover, MovieInfoField.title])
    normalize_actress_name: bool = True
    respect_source_id: bool = True


class MovieDefault(BaseConfig):
    title: str
    actress: str
    series: str
    director: str
    producer: str
    publisher: str


class PathSummarize(BaseConfig):
    output_folder_pattern: str
    basename_pattern: str
    length_maximum: PositiveInt
    length_by_byte: bool
    max_actress_count: PositiveInt = 10
    hard_link: bool


class TitleSummarize(BaseConfig):
    remove_trailing_actor_name: bool


class NFOSummarize(BaseConfig):
    basename_pattern: str
    title_pattern: str
    custom_genres_fields: list[str]
    custom_tags_fields: list[str]


class ExtraFanartSummarize(BaseConfig):
    enabled: bool
    scrap_interval: Duration


class SlimefaceEngine(BaseConfig):
    name: Literal["slimeface"]


class CoverCrop(BaseConfig):
    engine: SlimefaceEngine | None = None
    on_id_pattern: list[str]


class CoverSummarize(BaseConfig):
    basename_pattern: str
    highres: bool
    add_label: bool
    crop: CoverCrop


class FanartSummarize(BaseConfig):
    basename_pattern: str


class Summarizer(BaseConfig):
    default: MovieDefault
    censor_options_representation: list[str]
    title: TitleSummarize
    move_files: bool = True
    match_subtitles: bool = True
    filemove_log: bool = False
    path: PathSummarize
    nfo: NFOSummarize
    cover: CoverSummarize
    fanart: FanartSummarize
    extra_fanarts: ExtraFanartSummarize


class TranslateRuntime(BaseConfig):
    threads: PositiveInt = 6
    threads_batch: PositiveInt = 6
    ctx_size: PositiveInt = 8192
    batch_size: PositiveInt = 512
    ubatch_size: PositiveInt = 128
    parallel: PositiveInt = 1
    n_gpu_layers: NonNegativeInt = 0
    startup_timeout: PositiveInt = 300


class TranslateGeneration(BaseConfig):
    temperature: float = Field(default=0.7, ge=0, le=2)
    top_p: float = Field(default=0.6, gt=0, le=1)
    top_k: PositiveInt = 20
    repeat_penalty: float = Field(default=1.05, ge=1)
    max_tokens: PositiveInt = 4096
    timeout: PositiveInt = 1800


class Translator(BaseConfig):
    model: Literal["hy_mt2_7b_q4_k_m", "hy_mt2_1_8b_q4_k_m"] = "hy_mt2_7b_q4_k_m"
    model_dir: Path = Path("/app/models")
    auto_download: bool = True
    runtime: TranslateRuntime = Field(default_factory=TranslateRuntime)
    generation: TranslateGeneration = Field(default_factory=TranslateGeneration)

    @model_validator(mode="before")
    @classmethod
    def reject_removed_options(cls, data):
        if isinstance(data, dict):
            removed = {"engine", "fields"} & data.keys()
            if removed:
                raise ValueError(
                    "旧翻译配置已移除: "
                    + ", ".join(sorted(removed))
                    + "。请删除这些键，改用 translator.model、model_dir、runtime 和 generation"
                )
        return data


class Other(BaseConfig):
    debug: bool = False


def _find_user_config() -> str | None:
    """查找用户配置文件 config.yml

    查找顺序（命中即返回）：
    1. 容器工作目录 /app
    2. 源码根目录（本地开发时使用）

    每个目录优先查找 config/config.yml，再查找 config.yml。
    Docker 中的用户配置位于 /app/config/config.yml。
    """
    candidates = [os.getcwd()]
    # 开发模式回退：源码根目录（javsp 包的上一级）
    candidates.append(str(Path(__file__).resolve().parent.parent))
    for dir_path in candidates:
        # 保留 Docker/既有部署的 config 子目录，同时兼容根目录配置。
        for filename in (USER_CONFIG_FILE, "config.yml"):
            candidate = os.path.join(dir_path, filename)
            if os.path.isfile(candidate):
                return candidate
    return None


def get_config_source(argv=None):
    """显式构建默认、用户、环境与 CLI 配置源，每次调用互相独立。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = ArgumentParser(prog="JavSP", description="使用 R18.dev 本地数据库的影片元数据整理工具",
                            formatter_class=RawTextHelpFormatter, allow_abbrev=False)
    parser.add_argument("-c", "--config", help="使用指定的配置文件")
    args, _ = parser.parse_known_args(argv)
    sources = [FileSource(file=resource_path(DEFAULT_CONFIG_FILE))]
    user_config = args.config if args.config is not None else _find_user_config()
    if user_config is not None:
        source = _user_config_source(user_config)
        if source is not None:
            sources.append(source)
    sources.append(EnvSource(prefix="JAVSP_", allow_all=True, nested_separator="__"))
    overrides = {}
    for index, argument in enumerate(argv):
        if argument.startswith("--o"):
            if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
                raise ConfigLoadError(f"配置覆盖参数缺少值: {argument}")
            target = overrides
            parts = argument[3:].split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = argv[index + 1]
    sources.append(DataSource(data=overrides))
    return sources


def load_config(argv=None):
    """入口调用的配置加载器；导入配置模型不会读取文件或解析 CLI。"""
    return Cfg(config_sources=get_config_source(argv))


class Cfg(BaseConfig):
    concurrency: Concurrency = Field(default_factory=Concurrency)
    scanner: Scanner
    network: Network
    r18dev: R18Dev = Field(default_factory=R18Dev)
    summarizer: Summarizer
    translator: Translator
    other: Other

    @model_validator(mode="before")
    @classmethod
    def ignore_removed_scraping_options(cls, data):
        """忽略旧抓取配置，只提示键名，不输出可能含凭据的配置值。"""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "crawler" in data:
            data.pop("crawler")
            _logger.warning(
                "crawler 配置已移除并忽略；通用规则请手动迁移到 r18dev.required_keys、"
                "r18dev.normalize_actress_name、r18dev.respect_source_id，旧值不会自动继承"
            )
        for section, key in (("r18dev", "enabled"), ("network", "proxy_free"), ("concurrency", "crawlers")):
            values = data.get(section)
            if isinstance(values, dict) and key in values:
                values = dict(values)
                values.pop(key)
                data[section] = values
                _logger.warning("%s.%s 配置已移除并忽略，请删除该键；影片元数据始终查询 R18.dev 本地库", section, key)
        return data
