"""从JavBus抓取数据"""

import logging
from urllib.parse import urlsplit

from lxml import etree

from javsp.config import Cfg, CrawlerID
from javsp.datatype import GenreMap, MovieInfo
from javsp.logging_config import configure_logging
from javsp.web.base import is_cloudflare_challenge, request_get, resp2html, xpath_first
from javsp.web.exceptions import CrawlerError, MovieNotFoundError, SiteBlocked

logger = logging.getLogger(__name__)
genre_map = GenreMap("data/genre_javbus.csv")
permanent_url = "https://www.javbus.com"
if Cfg().network.proxy_server is not None:
    base_url = permanent_url
else:
    base_url = str(Cfg().network.proxy_free[CrawlerID.javbus])

_SITE = "javbus"

# XPath选择器集中定义
XP = {
    "container": "//div[@class='container']",
    "title": "h3/text()",
    "cover": "//a[@class='bigImage']/img/@src",
    "preview_pics": "//div[@id='sample-waterfall']/a/@href",
    "info": "//div[@class='col-md-3 info']",
    "page_title": "/html/head/title/text()",
    "dvdid": "p/span[contains(text(), '識別碼')]",
    "date": "p/span[contains(text(), '發行日期')]",
    "duration": "p/span[contains(text(), '長度')]",
    "director": "p/span[contains(text(), '導演')]",
    "producer": "p/span[contains(text(), '製作商')]",
    "publisher": "p/span[contains(text(), '發行商')]",
    "serial": "p/span[contains(text(), '系列')]",
    "genre": "//span[@class='genre']/label/a",
    "actress": "//a[@class='avatar-box']/div/img",
}


def parse_data(movie: MovieInfo):
    """从网页抓取并解析指定番号的数据
    Args:
        movie (MovieInfo): 要解析的影片信息，解析后的信息直接更新到此变量内
    """
    url = f"{base_url}/{movie.dvdid}"
    resp = request_get(url, delay_raise=True)
    if is_cloudflare_challenge(resp):
        raise SiteBlocked(f"JavBus: 无法通过CloudFlare检测: {url}")
    # 旧版登录跳转的302响应可能带有影片数据；普通重定向通常只有空正文。
    html = None
    for previous in resp.history:
        if previous.status_code != 302 or not previous.content.strip():
            continue
        try:
            candidate = resp2html(previous)
        except etree.ParserError:
            continue
        page_title = candidate.xpath(XP["page_title"])
        if candidate.xpath(XP["info"]) or (page_title and page_title[0].startswith("404 Page Not Found!")):
            html = candidate
            break
    if html is None:
        if urlsplit(resp.url).path.rstrip("/") == "/doc/driver-verify":
            raise SiteBlocked(f"JavBus: 需要浏览器验证，无法抓取影片页面: {url}")
        if not resp.content.strip():
            raise CrawlerError(f"JavBus: 返回空页面: {resp.url}")
        html = resp2html(resp)
    # 引入登录验证后状态码不再准确，因此还要额外通过检测标题来确认是否发生了404
    page_title = html.xpath(XP["page_title"])
    if page_title and page_title[0].startswith("404 Page Not Found!"):
        raise MovieNotFoundError(__name__, movie.dvdid)

    container = xpath_first(html, XP["container"], label=_SITE)
    title = xpath_first(container, XP["title"], label=_SITE)
    cover = xpath_first(container, XP["cover"], label=_SITE)
    preview_pics = container.xpath(XP["preview_pics"])
    info = xpath_first(container, XP["info"], label=_SITE)
    dvdid = xpath_first(info, XP["dvdid"], label=_SITE).getnext().text
    publish_date = xpath_first(info, XP["date"], label=_SITE).tail.strip()
    duration = xpath_first(info, XP["duration"], label=_SITE).tail.replace("分鐘", "").strip()
    director_tag = xpath_first(info, XP["director"], required=False, label=_SITE)
    if director_tag is not None:
        movie.director = director_tag.getnext().text.strip()
    producer_tag = xpath_first(info, XP["producer"], required=False, label=_SITE)
    if producer_tag is not None:
        text = producer_tag.getnext().text
        if text:
            movie.producer = text.strip()
    publisher_tag = xpath_first(info, XP["publisher"], required=False, label=_SITE)
    if publisher_tag is not None:
        movie.publisher = publisher_tag.getnext().text.strip()
    serial_tag = xpath_first(info, XP["serial"], required=False, label=_SITE)
    if serial_tag is not None:
        movie.serial = serial_tag.getnext().text
    # genre, genre_id
    genre_tags = info.xpath(XP["genre"])
    genre, genre_id = [], []
    for tag in genre_tags:
        tag_url = tag.get("href")
        pre_id = tag_url.split("/")[-1]
        genre.append(tag.text)
        if "uncensored" in tag_url:
            genre_id.append("uncensored-" + pre_id)
        else:
            genre_id.append(pre_id)
    movie.uncensored = any("uncensored" in tag.get("href", "") for tag in genre_tags) if genre_tags else None
    # JavBus的磁力链接是依赖js脚本加载的，无法通过静态网页来解析
    # actress, actress_pics
    actress, actress_pics = [], {}
    actress_tags = html.xpath(XP["actress"])
    for tag in actress_tags:
        name = tag.get("title")
        pic_url = tag.get("src")
        actress.append(name)
        if not pic_url.endswith("nowprinting.gif"):  # 略过默认的头像
            actress_pics[name] = pic_url
    # 整理数据并更新movie的相应属性
    movie.url = f"{permanent_url}/{movie.dvdid}"
    movie.dvdid = dvdid
    movie.title = title.replace(dvdid, "").strip()
    movie.cover = cover
    movie.preview_pics = preview_pics
    if publish_date != "0000-00-00":  # 丢弃无效的发布日期
        movie.publish_date = publish_date
    movie.duration = duration if int(duration) else None
    movie.genre = genre
    movie.genre_id = genre_id
    movie.actress = actress
    movie.actress_pics = actress_pics


def parse_clean_data(movie: MovieInfo):
    """解析指定番号的影片数据并进行清洗"""
    parse_data(movie)
    movie.genre_norm = genre_map.map(movie.genre_id)
    movie.genre_id = None  # 没有别的地方需要再用到，清空genre id（暗示已经完成转换）


if __name__ == "__main__":
    configure_logging(debug=True)

    movie = MovieInfo("NANP-030")
    try:
        parse_clean_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=True)
