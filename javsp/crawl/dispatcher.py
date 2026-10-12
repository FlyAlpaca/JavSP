"""从已准备的 R18.dev 本地数据库查询影片元数据。"""

import logging

from javsp.crawl.r18dev import SOURCE, lookup
from javsp.datatype import Movie
from javsp.workflow.models import CrawlResult

logger = logging.getLogger(__name__)


class CrawlerExecutor:
    """仅查询本地数据库，不创建站点会话或抓取线程。"""

    def __init__(self, cfg, *, runtime=None, resources=None):
        self.cfg = cfg
        self.resources = resources
        self.local_path = runtime.database_path if runtime is not None else None

    def crawl(self, movie: Movie, progress=None) -> CrawlResult:
        if self.resources:
            self.resources.check()
        local = None
        failures = []
        if self.local_path is None:
            failures.append((SOURCE, "本地数据库未初始化，请先完成启动准备"))
        else:
            try:
                local = lookup(self.local_path, movie)
            except Exception as error:
                logger.warning("R18.dev 本地查询失败: %s", error)
                failures.append((SOURCE, str(error)))
        if self.resources:
            self.resources.check()
        if local is None and not failures:
            failures.append((SOURCE, "本地数据库未找到匹配的影片记录（可能未收录或番号不匹配）"))
        if progress is not None and hasattr(progress, "set_description"):
            progress.set_description("R18.dev: 本地查询完成" if local else "R18.dev: 本地查询失败")
        return CrawlResult({SOURCE: local} if local else {}, failures, movie.dvdid, movie.cid, movie.data_src)
