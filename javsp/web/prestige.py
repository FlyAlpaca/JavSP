"""从蚊香社-prestige抓取数据"""

import logging
import re

from javsp.datatype import MovieInfo
from javsp.web.base import request_get, resp2html, xpath_first
from javsp.web.exceptions import CrawlerError, MovieNotFoundError, SiteBlocked

logger = logging.getLogger(__name__)
base_url = "https://www.prestige-av.com"
# prestige要求访问者携带已通过R18认证的cookies才能够获得完整数据，否则会被重定向到认证页面
# （其他多数网站的R18认证只是在网页上遮了一层，完整数据已经传回，不影响爬虫爬取）
cookies = {"__age_auth__": "true"}

_SITE = "prestige"

# XPath选择器集中定义
XP = {
    "container": "//section[@class='px-4 mb-4 md:px-8 md:mb-16']",
    "title": "h1/span",
    "cover": "//div[@class='c-ratio-image mr-8']/picture/source/img/@src",
    "actress": "//p[text()='出演者：']/following-sibling::div/p/a/text()",
    "duration": "//p[text()='収録時間：']",
    "date_url": "//p[text()='発売日：']/following-sibling::div/a/@href",
    "producer": "//p[text()='メーカー：']/following-sibling::div/a/text()",
    "dvdid": "//p[text()='品番：']/following-sibling::div/p/text()",
    "genre": "//p[text()='ジャンル：']/following-sibling::div/a",
    "serial": "//p[text()='レーベル：']/following-sibling::div/a/text()",
    "plot": "//h2[text()='商品紹介']/following-sibling::div/p",
    "preview_pics": "//h2[text()='サンプル画像']/following-sibling::div/div/picture/source/img/@src",
}


def parse_data(movie: MovieInfo):
    """从网页抓取并解析指定番号的数据
    Args:
        movie (MovieInfo): 要解析的影片信息，解析后的信息直接更新到此变量内
    """
    url = f"{base_url}/goods/goods_detail.php?sku={movie.dvdid}"
    resp = request_get(url, cookies=cookies, delay_raise=True)
    if resp.status_code == 500:
        # 500错误表明prestige没有这部影片的数据，不是网络问题，因此不再重试
        raise MovieNotFoundError(__name__, movie.dvdid)
    elif resp.status_code == 403:
        raise SiteBlocked("prestige不允许从当前IP所在地区访问，请尝试更换为日本地区代理")
    resp.raise_for_status()
    html = resp2html(resp)
    container_tags = html.xpath(XP["container"])
    if not container_tags:
        raise MovieNotFoundError(__name__, movie.dvdid)

    container = container_tags[0]
    title = xpath_first(container, XP["title"], label=_SITE).tail.strip()
    cover = xpath_first(container, XP["cover"], label=_SITE)
    cover = cover.split("?")[0]
    actress = container.xpath(XP["actress"])
    # 移除女优名中的空格，使女优名与其他网站保持一致
    actress = [i.strip().replace(" ", "") for i in actress]
    duration_str = xpath_first(container, XP["duration"], label=_SITE).getnext().text_content()
    match = re.search(r"\d+", duration_str)
    if match:
        movie.duration = match.group(0)
    date_url = xpath_first(container, XP["date_url"], label=_SITE)
    publish_date = date_url.split("?date=")[-1]
    producer = xpath_first(container, XP["producer"], label=_SITE).strip()
    dvdid = xpath_first(container, XP["dvdid"], label=_SITE)
    genre_tags = container.xpath(XP["genre"])
    genre = [tag.text.strip() for tag in genre_tags]
    serial = xpath_first(container, XP["serial"], label=_SITE).strip()
    plot = xpath_first(container, XP["plot"], label=_SITE).text.strip()
    preview_pics = container.xpath(XP["preview_pics"])
    preview_pics = [i.split("?")[0] for i in preview_pics]

    # prestige改版后已经无法获取高清封面，此前已经获取的高清封面地址也已失效
    movie.url = url
    movie.dvdid = dvdid
    movie.title = title
    movie.cover = cover
    movie.actress = actress
    movie.publish_date = publish_date
    movie.producer = producer
    movie.genre = genre
    movie.serial = serial
    movie.plot = plot
    movie.preview_pics = preview_pics
    movie.uncensored = False  # prestige服务器在日本且面向日本国内公开发售，不会包含无码片


if __name__ == "__main__":

    logger.root.handlers[1].level = logging.DEBUG

    movie = MovieInfo("ABP-647")
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=True)
