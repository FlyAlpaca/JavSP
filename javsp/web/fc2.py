"""从FC2官网抓取数据"""

import logging

from javsp.config import Cfg
from javsp.datatype import MovieInfo
from javsp.lib import strftime_to_minutes
from javsp.web.base import get_html, get_list_first, request_get, resp2html, select_fc2_cover, xpath_first
from javsp.web.exceptions import CrawlerError, MovieNotFoundError, SiteBlocked

logger = logging.getLogger(__name__)
base_url = "https://adult.contents.fc2.com"

_SITE = "fc2"

# XPath选择器集中定义
XP = {
    "container": "//div[@class='items_article_left']",
    "title": "//div[@class='items_article_headerInfo']/h3/text()",
    "thumb": "//div[@class='items_article_MainitemThumb']",
    "thumb_pic": "span/img/@src",
    "duration": "span/p[@class='items_article_info']/text()",
    "producer": "//li[starts-with(text(),'by')]/a/text()",
    "genre": "//a[@class='tag tagTag']/text()",
    "date": "//div[@class='items_article_softDevice']/p/text()",
    "preview_pics": "//ul[@data-feed='sample-images']/li/a/@href",
    "review_li": "//ul[@class='items_comment_headerReviewInArea']/li",
    "review_score": "div/span/text()",
    "review_vote": "span",
    "score_attr": "//a[@class='items_article_Stars']/p/span/@class",
    "desc_iframe": "//section[@class='items_article_Contents']/iframe/@src",
}


def get_movie_score(fc2_id):
    """通过评论数据来计算FC2的影片评分（10分制），无法获得评分时返回None"""
    html = get_html(f"{base_url}/article/{fc2_id}/review")
    review_tags = html.xpath(XP["review_li"])
    reviews = {}
    for tag in review_tags:
        score_tag = get_list_first(tag.xpath(XP["review_score"]))
        vote_tag = get_list_first(tag.xpath(XP["review_vote"]))
        if score_tag is None or vote_tag is None:
            continue
        try:
            score = int(score_tag)
            vote = int(vote_tag.text_content())
            reviews[score] = vote
        except ValueError:
            continue
    total_votes = sum(reviews.values())
    if total_votes >= 2:  # 至少也该有两个人评价才有参考意义一点吧
        summary = sum([k * v for k, v in reviews.items()])
        final_score = summary / total_votes * 2  # 乘以2转换为10分制
        return final_score


def parse_data(movie: MovieInfo):
    """解析指定番号的影片数据"""
    # 去除番号中的'FC2'字样
    id_uc = movie.dvdid.upper()
    if not id_uc.startswith("FC2-"):
        raise ValueError("Invalid FC2 number: " + movie.dvdid)
    fc2_id = id_uc.replace("FC2-", "")
    # 抓取网页
    url = f"{base_url}/article/{fc2_id}/"
    resp = request_get(url)
    if "/id.fc2.com/" in resp.url:
        raise SiteBlocked("FC2要求当前IP登录账号才可访问，请尝试更换为日本IP")
    html = resp2html(resp)
    container = xpath_first(html, XP["container"], required=False, label=_SITE)
    if container is None:
        raise MovieNotFoundError(__name__, movie.dvdid)
    # FC2 标题增加反爬乱码，使用数组合并标题
    title_arr = container.xpath(XP["title"])
    title = "".join(title_arr)
    thumb_tags = container.xpath(XP["thumb"])
    thumb_tag = thumb_tags[0] if thumb_tags else None
    thumb_pic = get_list_first(thumb_tag.xpath(XP["thumb_pic"])) if thumb_tag is not None else None
    duration_str = get_list_first(thumb_tag.xpath(XP["duration"])) if thumb_tag is not None else None
    # FC2没有制作商和发行商的区分，作为个人市场，影片页面的'by'更接近于制作商
    producer = get_list_first(container.xpath(XP["producer"]))
    genre = container.xpath(XP["genre"])
    date_str = get_list_first(container.xpath(XP["date"]))  # '販売日 : 2017/11/30'
    publish_date = date_str[-10:].replace("/", "-") if date_str else None
    preview_pics = container.xpath(XP["preview_pics"])

    if Cfg().crawler.hardworking:
        # 通过评论数据来计算准确的评分
        score = get_movie_score(fc2_id)
        if score is not None:
            movie.score = f"{score:.2f}"
        # 预览视频是动态加载的，不在静态网页中
        desc_frame_url = get_list_first(container.xpath(XP["desc_iframe"]))
        if desc_frame_url:
            key = desc_frame_url.split("=")[-1]
            api_url = f"{base_url}/api/v2/videos/{fc2_id}/sample?key={key}"
            try:
                r = request_get(api_url).json()
                movie.preview_video = r.get("path")
            except Exception:
                logger.debug(f"FC2获取预览视频失败: {fc2_id}", exc_info=True)
    else:
        # 获取影片评分。影片页面的评分只能粗略到星级，且没有分数，要通过类名来判断，如'items_article_Star5'表示5星
        score_tag_attr = get_list_first(container.xpath(XP["score_attr"]))
        if score_tag_attr:
            try:
                score = int(score_tag_attr[-1]) * 2
                movie.score = f"{score:.2f}"
            except (ValueError, IndexError):
                logger.debug(f"FC2评分解析失败: {score_tag_attr}")

    movie.dvdid = id_uc
    movie.url = url
    movie.title = title
    movie.genre = genre
    movie.producer = producer
    if duration_str:
        movie.duration = str(strftime_to_minutes(duration_str))
    movie.publish_date = publish_date
    movie.preview_pics = preview_pics
    # FC2的封面是220x220的，和正常封面尺寸、比例都差太多。如果有预览图片，则使用第一张预览图作为封面
    select_fc2_cover(movie)
    if not movie.cover:
        movie.cover = thumb_pic


if __name__ == "__main__":
    logger.root.handlers[1].level = logging.DEBUG

    movie = MovieInfo("FC2-718323")
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=True)
