"""从arzon抓取数据"""

import logging
import re

from javsp.web.base import request_get, read_proxy
from javsp.web.exceptions import *
from javsp.datatype import MovieInfo
import requests
from lxml import html

logger = logging.getLogger(__name__)
base_url = "https://www.arzon.jp"

# arzon有两种搜索模式：AV和Image Video，字段名不同
_SEARCH_MODES = {
    "arzon": {
        "search_url": "/itemlist.html?t=&m=all&s=&q={id}",
        "title_xpath": "//div[@class='detail_title_new2']//h1/text()",
        "field_map": {
            "actress": "AV女優：",
            "producer": "AVメーカー：",
            "video_type": "AVレーベル：",
        },
    },
    "arzon_iv": {
        "search_url": "/imagelist.html?q={id}",
        "title_xpath": "//div[@class='detail_title_new']//h1/text()",
        "field_map": {
            "actress": "タレント：",
            "producer": "イメージメーカー：",
            "video_type": "イメージレーベル：",
        },
    },
}


def get_cookie():
    skip_verify_url = (
        "http://www.arzon.jp/index.php?action=adult_customer_agecheck&agecheck=1"
    )
    session = requests.Session()
    session.get(skip_verify_url, timeout=(12, 7), proxies=read_proxy())
    return session.cookies.get_dict()


def parse_data(movie: MovieInfo, mode="arzon"):
    """解析指定番号的影片数据"""
    cfg = _SEARCH_MODES[mode]
    cookies = get_cookie()
    url = f"{base_url}{cfg['search_url'].format(id=movie.dvdid)}"
    r = request_get(url, cookies, delay_raise=True)
    if r.status_code == 404:
        raise MovieNotFoundError(__name__, movie.dvdid)
    data = html.fromstring(r.content)

    urls = data.xpath("//h2/a/@href")
    if len(urls) == 0:
        raise MovieNotFoundError(__name__, movie.dvdid)

    item_url = base_url + urls[0]
    e = request_get(item_url, cookies, delay_raise=True)
    item = html.fromstring(e.content)

    title = item.xpath(cfg["title_xpath"])[0]
    cover = item.xpath("//td[@align='center']//a/img/@src")[0]
    item_text = item.xpath("//div[@class='item_text']/text()")
    plot = [i.strip() for i in item_text if i.strip() != ""][0]
    preview_pics_arr = item.xpath("//div[@class='detail_img']//img/@src")
    preview_pics = [("https:" + u).replace("m_", "") for u in preview_pics_arr]

    container = item.xpath("//div[@class='item_register']/table//tr")
    genre = None
    video_type = None
    for row in container:
        key = row.xpath("./td[1]/text()")[0]
        contents = row.xpath("./td[2]//text()")
        content = [i.strip() for i in contents if i.strip() != ""]
        value = content[0] if content else None

        if key == cfg["field_map"]["actress"]:
            movie.actress = content
        elif key == cfg["field_map"]["producer"]:
            movie.producer = value
        elif key == cfg["field_map"]["video_type"]:
            video_type = value
        elif key == "シリーズ：" and mode == "arzon":
            movie.serial = value
        elif key == "監督：":
            movie.director = value
        elif key == "発売日：" and value:
            movie.publish_date = (
                re.search(r"\d{4}/\d{2}/\d{2}", value).group(0).replace("/", "-")
            )
        elif key == "収録時間：" and value:
            movie.duration = re.search(r"([\d.]+)分", value).group(1)
        elif key == "品番：":
            pass  # dvd_id = value
        elif key == "タグ：":
            genre = value

    genres = []
    if video_type:
        genres = [video_type]
    if genre is not None:
        genres.append(genre)

    movie.genre = genres
    movie.url = item_url
    movie.title = title
    movie.plot = plot
    movie.cover = f"https:{cover}"
    if preview_pics:
        movie.preview_pics = preview_pics


if __name__ == "__main__":
    import pretty_errors

    pretty_errors.configure(display_link=True)
    logger.root.handlers[1].level = logging.DEBUG

    movie = MovieInfo("csct-011")
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=True)
