import logging
from argparse import ArgumentParser, RawTextHelpFormatter
from enum import Enum
from pathlib import Path
from typing import Literal, TypeAlias

from confz import BaseConfig, CLArgSource, EnvSource, FileSource
from pydantic import ByteSize, Field, NonNegativeInt, PositiveInt, model_validator
from pydantic_core import Url
from pydantic_extra_types.pendulum_dt import Duration

from javsp.lib import resource_path

_logger = logging.getLogger(__name__)

DEFAULT_CONFIG_FILE = "config_default.yml"
USER_CONFIG_FILE = "config.yml"


class Scanner(BaseConfig):
    ignored_id_pattern: list[str]
    input_directory: Path | None = None
    filename_extensions: list[str]
    ignored_folder_name_pattern: list[str]
    minimum_size: ByteSize
    skip_nfo_dir: bool
    manual: bool


class CrawlerID(str, Enum):
    avsox = "avsox"
    avwiki = "avwiki"
    dl_getchu = "dl_getchu"
    fanza = "fanza"
    fc2 = "fc2"
    fc2fan = "fc2fan"
    fc2ppvdb = "fc2ppvdb"
    gyutto = "gyutto"
    jav321 = "jav321"
    javbus = "javbus"
    javdb = "javdb"
    javlib = "javlib"
    javmenu = "javmenu"
    mgstage = "mgstage"
    njav = "njav"
    prestige = "prestige"


class Network(BaseConfig):
    proxy_server: Url | None
    retry: NonNegativeInt = 3
    timeout: Duration
    proxy_free: dict[str, Url]

    @model_validator(mode="before")
    @classmethod
    def filter_invalid_proxy_free(cls, data):
        valid_keys = {e.value for e in CrawlerID}
        if isinstance(data, dict):
            proxy_free = data.get("proxy_free", {})
            invalid_keys = [k for k in proxy_free if k not in valid_keys]
            if invalid_keys:
                _logger.warning(f"配置的免代理站点无效，已自动忽略: {', '.join(invalid_keys)}")
                data["proxy_free"] = {k: v for k, v in proxy_free.items() if k in valid_keys}
        return data


class CrawlerSelect(BaseConfig):
    normal: list[str]
    fc2: list[str]
    cid: list[str]
    getchu: list[str]
    gyutto: list[str]

    @model_validator(mode="before")
    @classmethod
    def filter_invalid_crawlers(cls, data):
        valid_ids = {e.value for e in CrawlerID}
        all_invalid = []
        if isinstance(data, dict):
            for attr in ("normal", "fc2", "cid", "getchu", "gyutto"):
                original = data.get(attr, [])
                valid = [c for c in original if c in valid_ids]
                invalid = [c for c in original if c not in valid_ids]
                if invalid:
                    all_invalid.extend(invalid)
                data[attr] = valid
            if all_invalid:
                _logger.warning(f"配置的抓取器无效，已自动忽略: {', '.join(all_invalid)}")
        return data

    def items(self) -> list[tuple[str, list[str]]]:
        return [
            ("normal", self.normal),
            ("fc2", self.fc2),
            ("cid", self.cid),
            ("getchu", self.getchu),
            ("gyutto", self.gyutto),
        ]

    def __getitem__(self, index) -> list[str]:
        match index:
            case "normal":
                return self.normal
            case "fc2":
                return self.fc2
            case "cid":
                return self.cid
            case "getchu":
                return self.getchu
            case "gyutto":
                return self.gyutto
        raise Exception("Unknown crawler type")


class MovieInfoField(str, Enum):
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


class UseJavDBCover(str, Enum):
    yes = "yes"
    no = "no"
    fallback = "fallback"


class Crawler(BaseConfig):
    selection: CrawlerSelect
    required_keys: list[MovieInfoField]
    hardworking: bool
    respect_site_avid: bool
    fc2fan_local_path: Path | None
    sleep_after_scraping: Duration
    use_javdb_cover: UseJavDBCover
    normalize_actress_name: bool


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
    path: PathSummarize
    nfo: NFOSummarize
    cover: CoverSummarize
    fanart: FanartSummarize
    extra_fanarts: ExtraFanartSummarize


class OpenAICompatibleEngine(BaseConfig):
    type: Literal["openai_compatible"]
    base_url: Url
    api_key: str
    model: str
    system_prompt: str = (
        "Translate the following Japanese text into Chinese. "
        "Keep non-Japanese text, names, and any content that does not look like Japanese unchanged. "
        "Reply with the translated text only, do not add any text that is not in the original content."
    )
    temperature: float = 0.3
    max_tokens: int = 2048


class AnthropicEngine(BaseConfig):
    type: Literal["anthropic"]
    base_url: Url = Url("https://api.anthropic.com/v1")
    api_key: str
    model: str = "claude-3-5-sonnet-20241022"
    system_prompt: str = (
        "Translate the following Japanese text into Chinese. "
        "Keep non-Japanese text, names, and any content that does not look like Japanese unchanged. "
        "Reply with the translated text only, do not add any text that is not in the original content."
    )
    max_tokens: int = 2048
    temperature: float = 0.3


class GoogleTranslateEngine(BaseConfig):
    """Google 翻译（免费，无需 API Key）"""

    type: Literal["google"]
    source_language: str = "auto"
    target_language: str = "zh-CN"


class BingTranslateEngine(BaseConfig):
    """Bing 翻译（免费，无需 API Key）"""

    type: Literal["bing"]
    source_language: str = "auto"
    target_language: str = "zh-Hans"


class AlibabaTranslateEngine(BaseConfig):
    """阿里翻译（免费，无需 API Key）"""

    type: Literal["alibaba"]
    source_language: str = "auto"
    target_language: str = "zh"


TranslateEngine: TypeAlias = (
    OpenAICompatibleEngine | AnthropicEngine | GoogleTranslateEngine | BingTranslateEngine | AlibabaTranslateEngine | None
)


class TranslateField(BaseConfig):
    title: bool = True
    plot: bool = True


class Translator(BaseConfig):
    engine: TranslateEngine = Field(default=None, discriminator="type")
    fields: TranslateField = TranslateField()


class Other(BaseConfig):
    interactive: bool
    check_update: bool
    auto_update: bool


def get_config_source():
    parser = ArgumentParser(
        prog="JavSP",
        description="汇总多站点数据的AV元数据刮削器",
        formatter_class=RawTextHelpFormatter,
    )
    parser.add_argument("-c", "--config", help="使用指定的配置文件")
    args, _ = parser.parse_known_args()
    sources = []

    # 1. 始终加载默认模板配置
    default_config = resource_path(DEFAULT_CONFIG_FILE)
    sources.append(FileSource(file=default_config))

    # 2. 加载用户配置（如果存在），覆盖默认值
    if args.config is not None:
        # 用户通过 -c 指定了配置文件
        sources.append(FileSource(file=args.config))
    else:
        # 与默认配置同目录的 config.yml
        user_config = resource_path(USER_CONFIG_FILE)
        if Path(user_config).exists():
            sources.append(FileSource(file=user_config))

    # 3. 环境变量和命令行参数优先级最高
    sources.append(EnvSource(prefix="JAVSP_", allow_all=True))
    sources.append(CLArgSource(prefix="o"))
    return sources


class Cfg(BaseConfig):
    scanner: Scanner
    network: Network
    crawler: Crawler
    summarizer: Summarizer
    translator: Translator
    other: Other
    CONFIG_SOURCES = get_config_source()
