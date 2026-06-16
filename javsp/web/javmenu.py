"""从JavMenu抓取数据"""

import logging

from javsp.datatype import MovieInfo
from javsp.web.base import Request, resp2html, xpath_first
from javsp.web.exceptions import CrawlerError, MovieNotFoundError

request = Request()

logger = logging.getLogger(__name__)
base_url = "https://javmenu.com/zh"

_SITE = "javmenu"

# XPath选择器集中定义
XP = {
    "container": "//div[contains(@class, 'col-md-9')]",
    "title": ".//h1/strong/text()",
    "cover_video": ".//div[@id='primary-player']//video",
    "info": ".//div[@class='card-body']",
    "date": "div[contains(@class, 'd-flex')]/span[contains(text(), '发佈于:')]",
    "duration": "div[contains(@class, 'd-flex')]/span[contains(text(), '时长:')]",
    "producer": "div[@class='maker d-flex']/a/span/text()",
    "genre": ".//a[@class='genre']",
    "actress": "div[contains(@class, 'd-flex')][span[contains(text(), '女优:')]]//a/text()",
    "magnet_table": ".//table[contains(@class, 'magnet')]",
    "preview_pics": ".//a[@data-fancybox='gallery']/@href",
}


def parse_data(movie: MovieInfo):
    """从网页抓取并解析指定番号的数据
    Args:
        movie (MovieInfo): 要解析的影片信息，解析后的信息直接更新到此变量内
    """
    # JavMenu网页做得很不走心，将就了
    url = f"{base_url}/{movie.dvdid}"
    r = request.get(url)
    if r.history:
        # 被重定向到主页说明找不到影片资源
        raise MovieNotFoundError(__name__, movie.dvdid)

    html = resp2html(r)
    container = xpath_first(html, XP["container"], label=_SITE)
    title = container.xpath(XP["title"])[0]
    title = title.replace("免费AV在线看", "").replace("免費在線看", "").strip()
    video_tag = xpath_first(container, XP["cover_video"], required=False, label=_SITE)
    if video_tag is not None:
        poster = video_tag.get("poster") or video_tag.get("data-poster")
        if poster:
            movie.cover = poster.strip()
    info = xpath_first(container, XP["info"], label=_SITE)
    date_tag = xpath_first(info, XP["date"], required=False, label=_SITE)
    if date_tag is not None:
        publish_date = date_tag.getnext().text.strip()
    else:
        publish_date = None
    duration_tag = xpath_first(info, XP["duration"], required=False, label=_SITE)
    if duration_tag is not None:
        duration = duration_tag.getnext().text.replace("分钟", "").replace("分鐘", "").strip()
    else:
        duration = None
    producer = info.xpath(XP["producer"])
    if producer:
        movie.producer = producer[0].strip()
    genre_tags = container.xpath(XP["genre"])
    genre, genre_id = [], []
    for tag in genre_tags:
        items = tag.get("href").split("/")
        pre_id = items[-3] + "/" + items[-1]
        genre.append(tag.text.strip())
        genre_id.append(pre_id)
    actress = info.xpath(XP["actress"])
    actress = [a.strip() for a in actress if a.strip()] or None
    magnet_table = container.xpath(XP["magnet_table"])
    if magnet_table:
        magnet_links = magnet_table[0].xpath(".//tr/td/a/@href")
        movie.magnet = [i.replace("[javdb.com]", "") for i in magnet_links]
    preview_pics = container.xpath(XP["preview_pics"])

    if (not movie.cover) and preview_pics:
        movie.cover = preview_pics[0]
    movie.url = url
    movie.title = title.replace(movie.dvdid, "").strip()
    movie.preview_pics = preview_pics
    movie.publish_date = publish_date
    movie.duration = duration
    movie.genre = genre
    movie.genre_id = genre_id
    movie.actress = actress


if __name__ == "__main__":

    logger.root.handlers[1].level = logging.DEBUG

    movie = MovieInfo("FC2-718323")
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=True)
