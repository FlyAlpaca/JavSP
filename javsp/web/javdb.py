"""从JavDB抓取数据"""

import logging
import re

from javsp.chromium import get_browsers_cookies
from javsp.datatype import GenreMap, MovieInfo
from javsp.scan.avid import guess_av_type
from javsp.web.base import is_cloudflare_challenge, resp2html, xpath_first
from javsp.web.exceptions import (
    CredentialError,
    MovieDuplicateError,
    MovieNotFoundError,
    SiteBlocked,
    SitePermissionError,
    WebsiteError,
)

# 初始化Request实例。使用scraper绕过CloudFlare后，需要指定网页语言，否则可能会返回其他语言网页，影响解析

logger = logging.getLogger(__name__)
genre_map = GenreMap("data/genre_javdb.csv")
permanent_url = "https://javdb.com"

_SITE = "javdb"

# XPath选择器集中定义
XP = {
    # 搜索结果页
    "search_title": "//div[@class='video-title']/strong/text()",
    "search_url": "//a[@class='box']/@href",
    "search_box": "//a[@class='box']",
    "search_score": "div[@class='score']/span/span",
    "search_meta": "div[@class='meta']/text()",
    # 详情页
    "detail": "/html/body/section/div/div[@class='video-detail']",
    "info_panel": "//nav[@class='panel movie-panel-info']",
    "title": "h2/strong[@class='current-title']/text()",
    "ori_title_link": "//a[contains(@class, 'meta-link') and not(contains(@style, 'display: none'))]",
    "ori_title": "h2/span[@class='origin-title']/text()",
    "cover": "//img[@class='video-cover']/@src",
    "preview_pics": "//a[@class='tile-item'][@data-fancybox='gallery']/@href",
    "preview_video": "//video[@id='preview-video']/source/@src",
    "info_dvdid": "div/span",
    "info_date": "div/strong[text()='日期:']",
    "info_duration": "div/strong[text()='時長:']",
    "info_director": "div/strong[text()='導演:']",
    "info_producer": "div/strong[text()='片商:']",
    "info_seller": "div/strong[text()='賣家:']",
    "info_publisher": "div/strong[text()='發行:']",
    "info_serial": "div/strong[text()='系列:']",
    "info_genre": "//strong[text()='類別:']/../span/a",
    "info_actor": "//strong[text()='演員:']/../span",
    "score_stars": "//span[@class='score-stars']",
    "magnet": "//div[@class='magnet-name column is-four-fifths']/a/@href",
    # CloudFlare检测
    "cf_code": "//span[@class='code-label']/span",
    # 用户信息
    "user_profile": "//div[@class='user-profile']/ul/li[1]/span/following-sibling::text()",
    "user_name": "//div[@class='user-profile']/ul/li[2]/span/following-sibling::text()",
    "user_bio": "//div[@class='p-note user-profile-bio mb-3 js-user-profile-bio']",
}


def get_html_wrapper(url, *, context):
    """包装外发的request请求并负责转换为可xpath的html，同时处理Cookies无效等问题"""
    r = context.request.get(url, delay_raise=True)
    if r.status_code == 200:
        # 发生重定向可能仅仅是域名重定向，因此还要检查url以判断是否被跳转到了登录页
        if r.history and "/login" in r.url:
            # 仅在需要时去读取Cookies
            if context.cookies_pool is None:
                try:
                    context.cookies_pool = get_browsers_cookies()
                except (PermissionError, OSError) as e:
                    logger.warning(
                        f"无法从浏览器Cookies文件获取JavDB的登录凭据({e})，可能是安全软件在保护浏览器Cookies文件",
                        exc_info=True,
                    )
                    context.cookies_pool = []
                except Exception as e:
                    logger.warning(
                        f"获取JavDB的登录凭据时出错({e})，你可能使用的是国内定制版等非官方Chrome系浏览器",
                        exc_info=True,
                    )
                    context.cookies_pool = []
            if len(context.cookies_pool) > 0:
                item = context.cookies_pool.pop()
                context.reset_request()
                context.request.cookies = item["cookies"]
                if context.request.scraper:
                    context.request.scraper.cookies.clear()
                    for k, v in item["cookies"].items():
                        context.request.scraper.cookies.set(k, v)
                cookies_source = (item["profile"], item["site"])
                logger.debug(f"未携带有效Cookies而发生重定向，尝试更换Cookies为: {cookies_source}")
                return get_html_wrapper(url, context=context)
            else:
                raise CredentialError("JavDB: 所有浏览器Cookies均已过期")
        elif r.history and "pay" in r.url.split("/")[-1]:
            raise SitePermissionError(f"JavDB: 此资源被限制为仅VIP可见: '{r.history[0].url}'")
        else:
            html = resp2html(r)
            return html
    elif r.status_code in (403, 503):
        if is_cloudflare_challenge(r):
            # CF挑战页面：无法通过，直接报错
            raise SiteBlocked(f"JavDB: 无法通过CloudFlare检测: {url}")
        # 非CF挑战的403/503：解析站点特定的错误码
        html = resp2html(r)
        code_tag = html.xpath(XP["cf_code"])
        error_code = code_tag[0].text if code_tag else None
        if error_code:
            if error_code == "1020":
                block_msg = f"JavDB: {r.status_code} 禁止访问: 站点屏蔽了来自日本地区的IP地址，请使用其他地区的代理服务器"
            else:
                block_msg = f"JavDB: {r.status_code} 禁止访问: {url} (Error code: {error_code})"
        else:
            block_msg = f"JavDB: {r.status_code} 禁止访问: {url}"
        raise SiteBlocked(block_msg)
    else:
        raise WebsiteError(f"JavDB: {r.status_code} 非预期状态码: {url}")


def get_user_info(site, cookies, *, context):
    """获取cookies对应的JavDB用户信息"""
    try:
        context.request.cookies = cookies
        html = context.request.get_html(f"https://{site}/users/profile")
    except Exception as e:
        logger.warning("JavDB: 获取用户信息时出错")
        logger.debug(e, exc_info=True)
        return
    # 扫描浏览器得到的Cookies对应的临时域名可能会过期，因此需要先判断域名是否仍然指向JavDB的站点
    if "JavDB" in html.text:
        email = xpath_first(html, XP["user_profile"], label=_SITE)
        username = xpath_first(html, XP["user_name"], label=_SITE)
        return email.strip(), username.strip()
    else:
        logger.debug("JavDB: 域名已过期: " + site)


def get_valid_cookies(*, context):
    """扫描浏览器，获取一个可用的Cookies"""
    # 经测试，Cookies所发往的域名不需要和登录时的域名保持一致，只要Cookies有效即可在多个域名间使用
    for d in context.cookies_pool:
        info = get_user_info(d["site"], d["cookies"], context=context)
        if info:
            return d["cookies"]
        else:
            logger.debug(f"{d['profile']}, {d['site']}: Cookies无效")


def parse_data(movie: MovieInfo, *, context):
    """从网页抓取并解析指定番号的数据
    Args:
        movie (MovieInfo): 要解析的影片信息，解析后的信息直接更新到此变量内
    """
    # JavDB搜索番号时会有多个搜索结果，从中查找匹配番号的那个
    html = get_html_wrapper(f"{context.base_url}/search?q={movie.dvdid}", context=context)
    ids = list(map(str.lower, html.xpath(XP["search_title"])))
    movie_urls = html.xpath(XP["search_url"])
    match_count = len([i for i in ids if i == movie.dvdid.lower()])
    if match_count == 0:
        raise MovieNotFoundError(__name__, movie.dvdid, ids)
    elif match_count == 1:
        index = ids.index(movie.dvdid.lower())
        new_url = movie_urls[index]
        try:
            html2 = get_html_wrapper(new_url, context=context)
        except (SitePermissionError, CredentialError):
            # 不开VIP不让看，过分。决定榨出能获得的信息，毕竟有时候只有这里能找到标题和封面
            box = html.xpath(XP["search_box"])[index]
            movie.url = new_url
            movie.title = box.get("title")
            movie.cover = box.xpath("div/img/@src")[0]
            score_str = xpath_first(box, XP["search_score"], label=_SITE).tail
            score = re.search(r"([\d.]+)分", score_str).group(1)
            movie.score = f"{float(score) * 2:.2f}"
            movie.publish_date = box.xpath(XP["search_meta"])[0].strip()
            return
    else:
        raise MovieDuplicateError(__name__, movie.dvdid, match_count)

    container = xpath_first(html2, XP["detail"], label=_SITE)
    info = xpath_first(container, XP["info_panel"], label=_SITE)
    title = xpath_first(container, XP["title"], label=_SITE)
    show_orig_title = container.xpath(XP["ori_title_link"])
    if show_orig_title:
        movie.ori_title = xpath_first(container, XP["ori_title"], label=_SITE)
    cover = xpath_first(container, XP["cover"], label=_SITE)
    preview_pics = container.xpath(XP["preview_pics"])
    preview_video_tag = container.xpath(XP["preview_video"])
    if preview_video_tag:
        preview_video = preview_video_tag[0]
        if preview_video.startswith("//"):
            preview_video = "https:" + preview_video
        movie.preview_video = preview_video
    dvdid = xpath_first(info, XP["info_dvdid"], label=_SITE).text_content()
    publish_date = xpath_first(info, XP["info_date"], label=_SITE).getnext().text
    duration_tag = xpath_first(info, XP["info_duration"], label=_SITE)
    duration = (
        duration_tag.getnext().text.replace("分鍾", "").strip()
        if duration_tag is not None and duration_tag.getnext() is not None and duration_tag.getnext().text
        else None
    )
    director_tag = xpath_first(info, XP["info_director"], required=False, label=_SITE)
    if director_tag is not None:
        movie.director = director_tag.getnext().text_content().strip()
    av_type = guess_av_type(movie.dvdid)
    if av_type != "fc2":
        producer_tag = xpath_first(info, XP["info_producer"], required=False, label=_SITE)
    else:
        producer_tag = xpath_first(info, XP["info_seller"], required=False, label=_SITE)
    if producer_tag is not None:
        movie.producer = producer_tag.getnext().text_content().strip()
    publisher_tag = xpath_first(info, XP["info_publisher"], required=False, label=_SITE)
    if publisher_tag is not None:
        movie.publisher = publisher_tag.getnext().text_content().strip()
    serial_tag = xpath_first(info, XP["info_serial"], required=False, label=_SITE)
    if serial_tag is not None:
        movie.serial = serial_tag.getnext().text_content().strip()
    score_tag = xpath_first(info, XP["score_stars"], required=False, label=_SITE)
    if score_tag is not None:
        score_str = score_tag.tail or ""
        score_match = re.search(r"([\d.]+)分", score_str)
        if score_match:
            movie.score = f"{float(score_match.group(1)) * 2:.2f}"
    genre_tags = info.xpath(XP["info_genre"])
    genre, genre_id = [], []
    censor_types = set()
    for tag in genre_tags:
        pre_id = tag.get("href").split("/")[-1]
        genre.append(tag.text)
        genre_id.append(pre_id)
        # 判定影片有码/无码
        subsite = pre_id.split("?")[0]
        if subsite in ("uncensored", "tags"):
            censor_types.add(subsite)
    movie.uncensored = True if "uncensored" in censor_types else False if "tags" in censor_types else None
    # JavDB目前同时提供男女优信息，根据用来标识性别的符号筛选出女优
    actors_tag = xpath_first(info, XP["info_actor"], label=_SITE)
    all_actors = actors_tag.xpath("a/text()")
    genders = actors_tag.xpath("strong/text()")
    actress = [actor for actor, gender in zip(all_actors, genders) if gender == "♀"]
    magnet = container.xpath(XP["magnet"])

    movie.dvdid = dvdid
    movie.url = new_url.replace(context.base_url, permanent_url)
    movie.title = title.replace(dvdid, "").strip()
    movie.cover = cover
    movie.preview_pics = preview_pics
    movie.publish_date = publish_date
    movie.duration = duration
    movie.genre = genre
    movie.genre_id = genre_id
    movie.actress = actress
    movie.magnet = [i.replace("[javdb.com]", "") for i in magnet]


def parse_clean_data(movie: MovieInfo, *, context):
    """解析指定番号的影片数据并进行清洗"""
    try:
        parse_data(movie, context=context)
        # 检查封面URL是否真的存在对应图片
        if movie.cover is not None:
            r = context.request.head(movie.cover)
            if r.status_code != 200:
                movie.cover = None
    except SiteBlocked:
        logger.error("JavDB: 可能触发了反爬虫机制，请稍后再试")
        raise
    if movie.genre_id and (not movie.genre_id[0].startswith("fc2?")):
        movie.genre_norm = genre_map.map(movie.genre_id)
        movie.genre_id = None  # 没有别的地方需要再用到，清空genre id（表明已经完成转换）
