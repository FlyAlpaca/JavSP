"""从av-wiki抓取数据"""

import logging

from javsp.datatype import MovieInfo
from javsp.web.base import request_get, resp2html, xpath_first
from javsp.web.exceptions import CrawlerError, MovieNotFoundError

logger = logging.getLogger(__name__)
base_url = "https://av-wiki.net"

_SITE = "avwiki"

# XPath选择器集中定义
XP = {
    "cover_img": "//header/div/a[@class='image-link-border']/img",
    "body": "//section[@class='article-body']",
    "title": "div[@class='blockquote-like']/p/text()",
    "info_table": "dl[@class='dltable']",
}


def parse_data(movie: MovieInfo):
    """从网页抓取并解析指定番号的数据
    Args:
        movie (MovieInfo): 要解析的影片信息，解析后的信息直接更新到此变量内
    """
    movie.url = url = f"{base_url}/{movie.dvdid}"
    resp = request_get(url, delay_raise=True)
    if resp.status_code == 404:
        raise MovieNotFoundError(__name__, movie.dvdid)
    html = resp2html(resp)

    cover_tag = html.xpath(XP["cover_img"])
    if cover_tag:
        try:
            srcset = cover_tag[0].get("srcset").split(", ")
            src_set_urls = {}
            for src in srcset:
                url, width = src.split()
                width = int(width.rstrip("w"))
                src_set_urls[width] = url
            max_pic = sorted(src_set_urls.items(), key=lambda x: x[0], reverse=True)
            movie.cover = max_pic[0][1]
        except Exception:
            movie.cover = cover_tag[0].get("src")
    body = xpath_first(html, XP["body"], label=_SITE)
    title = body.xpath(XP["title"])[0]
    title = title.replace(f"【{movie.dvdid}】", "")
    info = xpath_first(body, XP["info_table"], label=_SITE)
    dt_txt_ls, dd_tags = info.xpath("dt/text()"), info.xpath("dd")
    data = {}
    for dt_txt, dd in zip(dt_txt_ls, dd_tags):
        dt_txt = dt_txt.strip()
        a_tag = dd.xpath("a")
        if len(a_tag) == 0:
            dd_txt = dd.text.strip()
        else:
            dd_txt = [i.text.strip() for i in a_tag]
        if isinstance(dd_txt, list) and dt_txt != "AV女優名":  # 只有女优名以列表的数据格式保留
            dd_txt = dd_txt[0]
        data[dt_txt] = dd_txt

    ATTR_MAP = {
        "メーカー": "producer",
        "AV女優名": "actress",
        "メーカー品番": "dvdid",
        "シリーズ": "serial",
        "配信開始日": "publish_date",
    }
    for key, attr in ATTR_MAP.items():
        setattr(movie, attr, data.get(key))
    movie.title = title
    movie.uncensored = False  # 服务器在日本且面向日本国内公开发售，不会包含无码片


if __name__ == "__main__":


    movie = MovieInfo("259LUXU-593")
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=True)
