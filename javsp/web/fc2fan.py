"""解析fc2fan本地镜像的数据"""

# FC2官网的影片下架就无法再抓取数据，如果用户有fc2fan的镜像，那可以尝试从镜像中解析影片数据
import logging
import os
import re

import lxml.html

from javsp.datatype import MovieInfo
from javsp.web.base import resp2html
from javsp.web.exceptions import MovieNotFoundError, WebsiteError

logger = logging.getLogger(__name__)


def parse_data(movie: MovieInfo, *, context):
    """解析指定番号的影片数据"""
    if bool(context.cfg.crawler.fc2fan_local_path and context.cfg.crawler.fc2fan_local_path.exists()):
        html_file = f"{str(context.cfg.crawler.fc2fan_local_path)}/{movie.dvdid}.html"
        if not os.path.exists(html_file):
            raise MovieNotFoundError(__name__, movie.dvdid, html_file)
        html = lxml.html.parse(html_file)
    else:
        url = f"https://fc2club.top/html/{movie.dvdid}.html"
        req = context.request
        r = req.get(url, delay_raise=True)
        if r.status_code == 404:
            raise MovieNotFoundError(__name__, movie.dvdid)
        elif r.text == "":
            raise WebsiteError(f"fc2fan: 站点不可用 (HTTP {r.status_code}): {url}")
        html = resp2html(r)
    try:
        container = html.xpath("//div[@class='col-sm-8']")[0]
    except IndexError:
        raise WebsiteError("fc2fan: 站点不可用")
    title = container.xpath("h3/text()")[0]
    score_str = container.xpath("h5/strong[text()='影片评分']")[0].tail.strip()
    match = re.search(r"\d+", score_str)
    if match:
        score = int(match.group()) / 10  # fc2fan站长是按100分来打分的
        movie.score = f"{score:.1f}"
    resource_info = container.xpath("h5/strong[text()='资源参数']")[0].tail
    if "无码" in resource_info:
        movie.uncensored = True
    elif "有码" in resource_info:
        movie.uncensored = False
    # FC2没有制作商和发行商的区分，作为个人市场，卖家更接近于制作商
    producer = container.xpath("h5/strong[text()='卖家信息']")[0].getnext().text
    if producer:
        movie.producer = producer.strip()
    genre = container.xpath("h5/strong[text()='影片标签']/../a/text()")
    actress = container.xpath("h5/strong[text()='女优名字']/../a/text()")
    preview_pics = container.xpath("//ul[@class='slides']/li/img/@src")
    if bool(context.cfg.crawler.fc2fan_local_path and context.cfg.crawler.fc2fan_local_path.exists()):
        preview_pics = [os.path.normpath(os.path.join(str(context.cfg.crawler.fc2fan_local_path), i)) for i in preview_pics]
    # big_preview = container.xpath("//img[@id='thumbpic']/../@href")[0]    # 影片真实截图，目前暂时用不到

    movie.title = title
    movie.genre = genre
    movie.actress = actress
    if preview_pics:
        movie.preview_pics = preview_pics
        movie.cover = preview_pics[0]
