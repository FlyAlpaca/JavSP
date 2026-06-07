"""从fanza抓取数据

digital类型（videoa/anime/doujin等）页面已改为Next.js前端渲染，
静态HTML无法获取数据，因此使用GraphQL API获取。
mono/rental类型（dvd/ppr等）仍为传统HTML页面，继续使用XPath解析。
"""

import logging
import re

import requests as req_lib

from javsp.datatype import MovieInfo
from javsp.web.base import Request, read_proxy, resp2html, xpath_first
from javsp.web.exceptions import CrawlerError, MovieNotFoundError, SiteBlocked

logger = logging.getLogger(__name__)

base_url = "https://www.dmm.co.jp"
gql_url = "https://api.video.dmm.co.jp/graphql"

# 初始化Request实例（要求携带已通过R18认证的cookies，否则会被重定向到认证页面）
request = Request()
request.cookies = {"age_check_done": "1"}
request.headers["Accept-Language"] = "ja,en-US;q=0.9"

_SITE = "fanza"

# XPath选择器集中定义
XP = {
    # 搜索结果页（新版Tailwind CSS布局）
    "search_detail_links": '//a[contains(@href, "/detail/")]/@href',
    # mono/dvd详情页
    "mono_title": "//h1/text()",
    "mono_container": "//table[@class='mg-b12']/tr/td",
    "mono_cover": "//img[@name='package-image']/@src",
    "mono_cover_fallback": "//div[@id='sample-video']//img/@src",
    "mono_date": "//td[text()='発売日：']/following-sibling::td/text()",
    "mono_date_rental": "//td[text()='貸出開始日：']/following-sibling::td/text()",
    "mono_date_delivery": "//td[text()='配信開始日：']/following-sibling::td/text()",
    "mono_duration": "//td[text()='収録時間：']/following-sibling::td/text()",
    "mono_actress": "//span[@id='performer']/a/text()",
    "mono_director": "//td[text()='監督：']/following-sibling::td/a/text()",
    "mono_serial": "//td[text()='シリーズ：']/following-sibling::td/a/text()",
    "mono_producer": "//td[text()='メーカー：']/following-sibling::td/a/text()",
    "mono_genre": (
        "//td[text()='ジャンル：']/following-sibling::td/a[contains(@href,'?keyword=') or contains(@href,'article=keyword')]"
    ),
    "mono_cid": "//td[text()='品番：']/following-sibling::td/text()",
    "mono_plot": "//div[contains(@class, 'mg-b20 lh4')]/text()",
    "mono_preview_pics": "//a[@name='sample-image']/img/@src",
    "mono_preview_pics_lazy": "//a[@name='sample-image']/img/@data-lazy",
    "mono_score_img": "//td[text()='平均評価：']/following-sibling::td/img/@src",
}

# GraphQL查询定义
GQL_CONTENT_DETAIL = """query ContentDetail($id: ID!) {
  ppvContent(id: $id) {
    id
    title
    floor
    contentType
    packageImage { mediumUrl largeUrl }
    series { id name }
    maker { id name }
    actresses { id name }
    genres { id name }
    deliveryStartDate
    saleStartDate
    duration
    description
    sampleImages { imageUrl }
    directors { id name }
    label { id name }
  }
}"""

_GQL_HEADERS = {
    "Content-Type": "application/json",
    "Cookie": "age_check_done=1",
    "Origin": "https://video.dmm.co.jp",
    "Referer": "https://video.dmm.co.jp/",
}

_PRODUCT_PRIORITY = {"digital": 10, "mono": 5, "monthly": 2, "rental": 1}
_TYPE_PRIORITY = {
    "videoa": 10,
    "anime": 8,
    "nikkatsu": 6,
    "doujin": 4,
    "dvd": 3,
    "ppr": 2,
    "paradisetv": 1,
}

# digital类型需要通过GraphQL API获取数据
_DIGITAL_TYPES = {"videoa", "anime", "nikkatsu", "doujin", "paradisetv"}


def _gql_request(query, variables):
    """发送GraphQL请求"""
    payload = {"query": query, "variables": variables}
    r = req_lib.post(gql_url, json=payload, headers=_GQL_HEADERS, proxies=read_proxy())
    if r.status_code == 403:
        raise SiteBlocked("FANZA不允许从当前IP所在地区访问，请检查你的网络和代理服务器设置")
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        error_msg = data["errors"][0].get("message", "Unknown GraphQL error")
        raise CrawlerError(f"[{_SITE}] GraphQL error: {error_msg}")
    return data["data"]


def _parse_gql_content(content, movie):
    """从GraphQL返回的数据解析影片信息"""
    movie.cid = content["id"]
    movie.title = content.get("title", "")
    movie.cover = content.get("packageImage", {}).get("largeUrl")
    movie.big_cover = content.get("packageImage", {}).get("mediumUrl")

    # 日期：优先使用deliveryStartDate（配信開始日）
    date_str = content.get("deliveryStartDate")
    if date_str:
        movie.publish_date = date_str[:10]
    else:
        sale_date = content.get("saleStartDate")
        if sale_date:
            movie.publish_date = sale_date[:10]

    # 时长：GraphQL返回的是秒数
    duration_sec = content.get("duration")
    if duration_sec:
        movie.duration = str(duration_sec // 60)

    # 女优
    actresses = content.get("actresses", [])
    if actresses:
        movie.actress = [a["name"] for a in actresses]

    # 导演
    directors = content.get("directors", [])
    if directors:
        movie.director = directors[0]["name"]

    # 系列
    series = content.get("series")
    if series:
        movie.serial = series["name"]

    # 制作商
    maker = content.get("maker")
    if maker:
        movie.producer = maker["name"]

    # 标签
    genres = content.get("genres", [])
    if genres:
        movie.genre = [g["name"] for g in genres]
        movie.genre_id = [g["id"] for g in genres]

    # 简介
    description = content.get("description")
    if description:
        movie.plot = description.strip()

    # 预览图
    sample_images = content.get("sampleImages", [])
    if sample_images:
        movie.preview_pics = [img["imageUrl"] for img in sample_images]

    # FANZA是日本面向国内的服务，不会包含无码片
    movie.uncensored = False


def sort_search_result(result: list[dict]):
    """排序搜索结果"""
    scores = {
        i["url"]: (
            _PRODUCT_PRIORITY.get(i["product"], 0),
            _TYPE_PRIORITY.get(i["type"], 0),
        )
        for i in result
    }
    sorted_result = sorted(result, key=lambda x: scores[x["url"]], reverse=True)
    return sorted_result


def _search_cid_via_html(cid):
    """通过旧搜索页面查找cid对应的影片URL"""
    search_url = f"https://www.dmm.co.jp/search/=/searchstr={cid}/limit=30/sort=rankprofile"
    r = request.get(search_url)
    if r.status_code == 404:
        raise MovieNotFoundError(__name__, cid)
    html = resp2html(r)
    if "not available in your region" in html.text_content():
        raise SiteBlocked("FANZA不允许从当前IP所在地区访问，请检查你的网络和代理服务器设置")

    links = html.xpath(XP["search_detail_links"])
    parsed_result = {}
    for url in links:
        items = url.split("/")
        type_, found_cid = None, None
        for i, part in enumerate(items):
            if part == "-":
                product, type_ = items[i - 2], items[i - 1]
            elif part.startswith("cid="):
                found_cid = part[4:]
                clean_url = "/".join(p for p in items if not p.startswith("?")) + "/"
                parsed_result.setdefault(found_cid, []).append({"product": product, "type": type_, "url": clean_url})
                break

    if cid not in parsed_result:
        if len(links) > 0:
            logger.debug("Unknown URL in search result: " + ", ".join(links[:5]))
        raise MovieNotFoundError(__name__, cid)

    return sort_search_result(parsed_result[cid])


def _parse_mono_page(movie, html):
    """解析mono/dvd/rental类型的页面"""
    title = xpath_first(html, XP["mono_title"], _SITE)
    cover = html.xpath(XP["mono_cover"])
    if not cover:
        cover = html.xpath(XP["mono_cover_fallback"])
    cover = cover[0] if cover else None

    # 日期：依次尝试発売日、配信開始日、貸出開始日
    for date_xp in [XP["mono_date"], XP["mono_date_delivery"], XP["mono_date_rental"]]:
        date_tag = html.xpath(date_xp)
        if date_tag:
            movie.publish_date = date_tag[0].strip().replace("/", "-")
            break

    # 时长
    duration_tag = html.xpath(XP["mono_duration"])
    if duration_tag:
        match = re.search(r"\d+", duration_tag[0].strip())
        if match:
            movie.duration = match.group(0)

    # 女优
    actress = html.xpath(XP["mono_actress"])

    # 导演
    director_tag = html.xpath(XP["mono_director"])
    if director_tag:
        movie.director = director_tag[0].strip()

    # 系列
    serial_tag = html.xpath(XP["mono_serial"])
    if serial_tag:
        movie.serial = serial_tag[0].strip()

    # 制作商
    producer_tag = html.xpath(XP["mono_producer"])
    if producer_tag:
        movie.producer = producer_tag[0].strip()

    # 标签
    genre_tags = html.xpath(XP["mono_genre"])
    genre, genre_id = [], []
    for tag in genre_tags:
        genre.append(tag.text.strip())
        genre_id.append(tag.get("href").split("=")[-1].strip("/"))

    # 品番
    cid_tag = html.xpath(XP["mono_cid"])
    if cid_tag:
        movie.cid = cid_tag[0].strip()

    # 简介
    plot_tag = html.xpath(XP["mono_plot"])
    if plot_tag:
        movie.plot = plot_tag[0].strip()

    # 预览图
    preview_pics = html.xpath(XP["mono_preview_pics"])
    if not preview_pics:
        preview_pics = html.xpath(XP["mono_preview_pics_lazy"])

    # 评分
    score_img = html.xpath(XP["mono_score_img"])
    if score_img:
        score = int(score_img[0].split("/")[-1].split(".")[0])  # 00, 05 ... 50
        movie.score = f"{score / 5:.2f}"  # 转换为10分制

    movie.title = title
    movie.cover = cover
    movie.actress = actress
    movie.genre = genre
    movie.genre_id = genre_id
    movie.preview_pics = preview_pics
    movie.uncensored = False


def parse_data(movie: MovieInfo):
    """解析指定番号的影片数据"""
    # 策略1: 先尝试用GraphQL API直接查询（适用于digital/monthly类型）
    try:
        data = _gql_request(GQL_CONTENT_DETAIL, {"id": movie.cid})
        content = data.get("ppvContent")
        if content is not None:
            _parse_gql_content(content, movie)
            movie.url = f"{base_url}/digital/videoa/-/detail/=/cid={movie.cid}/"
            return
    except CrawlerError:
        logger.debug(f"GraphQL查询cid={movie.cid}失败，尝试搜索页面")

    # 策略2: 通过搜索页面查找影片URL
    try:
        urls = _search_cid_via_html(movie.cid)
    except MovieNotFoundError:
        raise MovieNotFoundError(__name__, movie.cid)

    # 对搜索结果按优先级排序后逐个尝试
    for d in urls:
        # digital/monthly类型优先用GraphQL API
        if d["type"] in _DIGITAL_TYPES or d["product"] == "monthly":
            try:
                data = _gql_request(GQL_CONTENT_DETAIL, {"id": movie.cid})
                content = data.get("ppvContent")
                if content is not None:
                    _parse_gql_content(content, movie)
                    movie.url = d["url"]
                    return
            except CrawlerError:
                logger.debug(f"GraphQL查询cid={movie.cid}失败")

        # mono/rental类型使用HTML解析
        try:
            r = request.get(d["url"])
            html = resp2html(r)
            if "not available in your region" in html.text_content():
                raise SiteBlocked("FANZA不允许从当前IP所在地区访问，请检查你的网络和代理服务器设置")
            _parse_mono_page(movie, html)
            movie.url = d["url"]
            return
        except CrawlerError, MovieNotFoundError:
            logger.debug(f"解析{d['url']}失败", exc_info=True)

    logger.warning(f"在fanza查找到的cid={movie.cid}的影片页面均解析失败")
    raise MovieNotFoundError(__name__, movie.cid)


if __name__ == "__main__":
    import pretty_errors

    pretty_errors.configure(display_link=True)
    logger.root.handlers[1].level = logging.DEBUG

    movie = MovieInfo(cid="ipx00177")
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=True)
