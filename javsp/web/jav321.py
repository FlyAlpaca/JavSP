"""从jav321抓取数据"""

import logging
import re

from javsp.datatype import MovieInfo
from javsp.web.base import post_html, xpath_first
from javsp.web.exceptions import CrawlerError, MovieNotFoundError

logger = logging.getLogger(__name__)
base_url = "https://www.jav321.com"

_SITE = "jav321"

# XPath选择器集中定义
XP = {
    "page_url": "//ul[@class='dropdown-menu']/li/a/@href",
    "title": "//div[@class='panel-heading']/h3/text()",
    "info": "//div[@class='col-md-9']",
    "company": "a[contains(@href,'/company/')]/text()",
    "actress_img": "//div[@class='thumbnail']/a[contains(@href,'/star/')]/img",
    "genre_tag": "a[contains(@href,'/genre/')]",
    "dvdid": "b[text()='品番']",
    "date": "b[text()='配信開始日']",
    "duration": "b[text()='収録時間']",
    "score_img": "//b[text()='平均評価']/following-sibling::img/@data-original",
    "serial": "a[contains(@href,'/series/')]/text()",
    "preview_video": "//video/source/@src",
    "plot": "//div[@class='panel-body']/div[@class='row']/div[@class='col-md-12']/text()",
    "preview_pics": "//div[@class='col-xs-12 col-md-12']/p/a/img[@class='img-responsive']/@src",
    "preview_pics_alt": "//div/div/div[@class='col-md-3']/img[@onerror and @class='img-responsive']/@src",
}


def parse_data(movie: MovieInfo):
    """解析指定番号的影片数据"""
    html = post_html(f"{base_url}/search", data={"sn": movie.dvdid})
    page_url = html.xpath(XP["page_url"])[0]
    # TODO: 注意cid是dmm的概念。如果影片来自MGSTAGE，这里的cid很可能是jav321自己添加的，例如 345SIMM-542
    cid = page_url.split("/")[-1]  # /video/ipx00177
    # 如果从URL匹配到的cid是'search'，说明还停留在搜索页面，找不到这部影片
    if cid == "search":
        raise MovieNotFoundError(__name__, movie.dvdid)
    title = html.xpath(XP["title"])[0]
    info = xpath_first(html, XP["info"], label=_SITE)
    # jav321的不同信息字段间没有明显分隔，只能通过url来匹配目标标签
    company_tags = info.xpath(XP["company"])
    if company_tags:
        movie.producer = company_tags[0]
    # actress, actress_pics
    # jav321现在连女优信息都没有了，首页通过女优栏跳转过去也全是空白
    actress, actress_pics = [], {}
    actress_tags = html.xpath(XP["actress_img"])
    for tag in actress_tags:
        name = tag.tail.strip()
        pic_url = tag.get("src")
        actress.append(name)
        # jav321的女优头像完全是应付了事：即使女优实际没有头像，也会有一个看起来像模像样的url，
        # 因而无法通过url判断女优头像图片是否有效。有其他选择时最好不要使用jav321的女优头像数据
        actress_pics[name] = pic_url
    # genre, genre_id
    genre_tags = info.xpath(XP["genre_tag"])
    genre, genre_id = [], []
    for tag in genre_tags:
        genre.append(tag.text)
        genre_id.append(tag.get("href").split("/")[-2])  # genre/4025/1
    dvdid = xpath_first(info, XP["dvdid"], label=_SITE).tail.replace(": ", "").upper()
    publish_date = xpath_first(info, XP["date"], label=_SITE).tail.replace(": ", "")
    duration_div = xpath_first(info, XP["duration"], required=False, label=_SITE)
    if duration_div is not None:
        match = re.search(r"\d+", duration_div.tail)
        if match:
            movie.duration = match.group(0)
    # 仅部分影片有评分且评分只能粗略到星级而没有分数，要通过星级的图片来判断，如'/img/35.gif'表示3.5星
    score_tag = info.xpath(XP["score_img"])
    if score_tag:
        score = int(score_tag[0][5:7]) / 5  # /10*2
        movie.score = str(score)
    serial_tag = info.xpath(XP["serial"])
    if serial_tag:
        movie.serial = serial_tag[0]
    preview_video_tag = info.xpath(XP["preview_video"])
    if preview_video_tag:
        movie.preview_video = preview_video_tag[0]
    plot_tag = info.xpath(XP["plot"])
    if plot_tag:
        movie.plot = plot_tag[0]
    preview_pics = html.xpath(XP["preview_pics"])
    if len(preview_pics) == 0:
        # 尝试搜索另一种布局下的封面，需要使用onerror过滤掉明明没有封面时网站往里面塞的默认URL
        preview_pics = html.xpath(XP["preview_pics_alt"])
    # 有的图片链接里有多个//，网站质量堪忧……
    preview_pics = [i[:8] + i[8:].replace("//", "/") for i in preview_pics]
    # 磁力和ed2k链接是依赖js脚本加载的，无法通过静态网页来解析

    movie.url = page_url
    movie.cid = cid
    movie.dvdid = dvdid
    movie.title = title
    movie.actress = actress
    movie.actress_pics = actress_pics
    movie.genre = genre
    movie.genre_id = genre_id
    movie.publish_date = publish_date
    # preview_pics的第一张图始终是封面，剩下的才是预览图
    if len(preview_pics) > 0:
        movie.cover = preview_pics[0]
        movie.preview_pics = preview_pics[1:]


if __name__ == "__main__":

    logger.root.handlers[1].level = logging.DEBUG

    movie = MovieInfo("SCUTE-1177")
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=True)
