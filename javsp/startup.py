"""批处理启动前的版本提醒与资源准备。"""

import logging
import re
from contextlib import contextmanager

import requests

from javsp.__version__ import get_version
from javsp.crawl.dispatcher import import_crawlers
from javsp.crawl.r18dev import prepare_database
from javsp.func import get_scan_dir
from javsp.metadata.actors import ActorMappingError
from javsp.metadata.summarizer import load_actress_aliases
from javsp.translate import LocalTranslator

logger = logging.getLogger(__name__)
LATEST_RELEASE_URL = "https://api.github.com/repos/FlyAlpaca/JavSP/releases/latest"
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?:\.dev\d+)?(?:\+[^\s]+)?$")


class StartupError(Exception):
    """启动前置条件不满足。"""


def _version_key(value):
    match = _VERSION.fullmatch(value)
    if match is None:
        return None
    numbers = tuple(int(part or 0) for part in match.groups()[:4])
    return (*numbers, -1 if ".dev" in value else 0)


def check_for_updates(*, session=requests):
    """提醒有新稳定版；网络或响应异常不影响离线批处理。"""
    current = get_version()
    logger.info("JavSP 当前版本: %s", current)
    if _version_key(current) is None or current == "0.0.0":
        logger.info("无法确定当前发布版本，跳过更新检查")
        return
    try:
        response = session.get(LATEST_RELEASE_URL, timeout=(2, 3))
        response.raise_for_status()
        latest = response.json().get("tag_name")
        if not isinstance(latest, str) or _version_key(latest) is None:
            raise ValueError("发布版本号缺失或格式异常")
    except (requests.RequestException, ValueError, AttributeError, TypeError) as error:
        logger.warning("JavSP 版本更新检查失败，继续使用当前版本: %s", error)
        return
    if _version_key(latest) > _version_key(current):
        logger.warning("发现 JavSP 新版本 %s（当前 %s），请在批处理结束后更新镜像或安装包", latest, current)
    else:
        logger.info("JavSP 已是最新稳定版本")


def prepare_startup(cfg):
    """统一检查配置依赖、数据源和运行资源，返回已验证的扫描目录。"""
    root = get_scan_dir(cfg.scanner.input_directory)
    if root is None:
        raise StartupError("扫描目录不存在或不是目录，请检查容器挂载和 scanner.input_directory")

    check_for_updates()
    try:
        load_actress_aliases(cfg)
    except ActorMappingError as error:
        raise StartupError(str(error)) from error
    if cfg.r18dev.enabled:
        cfg._r18dev_db_path = prepare_database(cfg._r18dev_db_path, cfg.crawler.online_enabled)
    import_crawlers(cfg)
    return root


@contextmanager
def startup_context(cfg):
    """按顺序准备数据源和模型，并负责关闭模型服务。"""
    root = prepare_startup(cfg)
    with LocalTranslator(cfg.translator) as translator:
        logger.info("启动前检查完成：配置、扫描目录、数据源和模型已就绪")
        yield root, translator
