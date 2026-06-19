"""从JavLibrary抓取数据"""

import logging
from urllib.parse import urlsplit

from javsp.config import Cfg, CrawlerID
from javsp.datatype import MovieInfo
from javsp.web.base import Request, is_cloudflare_challenge, read_proxy, resp2html, xpath_first
from javsp.web.exceptions import CrawlerError, MovieDuplicateError, MovieNotFoundError, SiteBlocked
from javsp.web.proxyfree import get_proxy_free_url

# 初始化Request实例
request = Request(use_scraper=True)

logger = logging.getLogger(__name__)
permanent_url = "https://www.javlibrary.com"
base_url = ""

_SITE = "javlib"

# XPath选择器集中定义
XP = {
    "search_video": "//div[@class='video'][@id]/a",
    "search_id": "div[@class='id']/text()",
    "rightcolumn": "/html/body/div/div[@id='rightcolumn']",
    "title": "div/h3/a/text()",
    "cover": "//img[@id='video_jacket_img']/@src",
    "video_info": "//div[@id='video_info']",
    "dvdid": "div[@id='video_id']//td[@class='text']/text()",
    "date": "div[@id='video_date']//td[@class='text']/text()",
    "duration": "div[@id='video_length']//span[@class='text']/text()",
    "director": "//span[@class='director']/a/text()",
    "producer": "//span[@class='maker']/a/text()",
    "publisher": "//span[@class='label']/a/text()",
    "score": "//span[@class='score']/text()",
    "genre": "//span[@class='genre']/a/text()",
    "actress": "//span[@class='star']/a/text()",
}


def _try_search(dvdid: str, preferred_url: str = None):
    """尝试搜索，返回 (resp, html) 或在所有地址被拦截时抛出异常

    地址优先级：preferred_url(重定向发现的新地址) > 免代理地址 > 代理访问永久域名
    """
    global base_url
    request.timeout = Cfg().network.timeout.total_seconds()
    # 候选地址列表：优先使用重定向发现的新地址，然后是免代理地址
    candidates = []
    if preferred_url:
        candidates.append((preferred_url, {}))
    proxy_free_url = get_proxy_free_url("javlib", str(Cfg().network.proxy_free[CrawlerID.javlib]))
    if proxy_free_url:
        candidates.append((proxy_free_url, {}))
    if Cfg().network.proxy_server:
        candidates.append((permanent_url, read_proxy()))
    else:
        # 无代理配置时直接尝试永久域名（直连）
        candidates.append((permanent_url, {}))

    failures = []
    for url, proxies in candidates:
        base_url = url
        request.proxies = proxies
        search_url = f"{url}/cn/vl_searchbyid.php?keyword={dvdid}"
        try:
            resp = request.get(search_url, delay_raise=True)
            if is_cloudflare_challenge(resp):
                failures.append(f"{url}: Cloudflare拦截")
                logger.debug(f"JavLib地址被拦截: {url}")
                continue
            html = resp2html(resp)
            # 验证页面包含 JavLib 特征内容
            if html.xpath("//div[@id='rightcolumn']") or html.xpath("//div[@id='video_title']"):
                return resp, html
            failures.append(f"{url}: 页面内容无效")
            logger.debug(f"JavLib地址返回无效内容: {url}")
        except Exception as e:
            failures.append(f"{url}: {e}")
            logger.debug(f"JavLib地址访问失败: {url}: {e}")
            continue

    detail = "; ".join(failures)
    raise SiteBlocked(f"JavLib: 所有地址均不可用 ({len(candidates)}个候选): {detail}")


# TODO: 发现JavLibrary支持使用cid搜索，会直接跳转到对应的影片页面，也许可以利用这个功能来做cid到dvdid的转换
def parse_data(movie: MovieInfo):
    """解析指定番号的影片数据"""
    global base_url
    # 直接使用_try_search，按优先级尝试所有地址（免代理优先）
    # 如果base_url已被重定向更新过，作为preferred_url传入避免被重置
    resp, html = _try_search(movie.dvdid, preferred_url=base_url if base_url else None)
    url = new_url = f"{base_url}/cn/vl_searchbyid.php?keyword={movie.dvdid}"
    # 判断是否发生了重定向：resp.history 非空（标准行为）或 resp.url 与请求 URL 不同（部分镜像的行为）
    is_redirected = bool(resp.history) or resp.url != url
    if is_redirected:
        if urlsplit(resp.url).netloc == urlsplit(base_url).netloc:
            # 重定向到同域名，说明搜索到了影片且只有一个结果
            new_url = resp.url
        else:
            # 重定向到了不同的netloc时，新地址并不是影片地址。这种情况下新地址中丢失了path字段，
            # 为无效地址（应该是JavBus重定向配置有问题），需要使用新的base_url抓取数据
            base_url = "https://" + urlsplit(resp.url).netloc
            logger.warning(f"请将配置文件中的JavLib免代理地址更新为: {base_url}")
            return parse_data(movie)
    else:  # 如果有多个搜索结果则不会自动跳转，此时需要程序介入选择搜索结果
        video_tags = html.xpath(XP["search_video"])
        # 通常第一部影片就是我们要找的，但是以免万一还是遍历所有搜索结果
        pre_choose = []
        for tag in video_tags:
            tag_dvdid = tag.xpath(XP["search_id"])[0]
            if tag_dvdid.upper() == movie.dvdid.upper():
                pre_choose.append(tag)
        pre_choose_urls = [i.get("href") for i in pre_choose]
        match_count = len(pre_choose)
        if match_count == 0:
            raise MovieNotFoundError(__name__, movie.dvdid)
        elif match_count == 1:
            new_url = pre_choose_urls[0]
        elif match_count == 2:
            no_blueray = []
            for tag in pre_choose:
                if "ブルーレイディスク" not in tag.get("title"):  # Blu-ray Disc
                    no_blueray.append(tag)
            no_blueray_count = len(no_blueray)
            if no_blueray_count == 1:
                new_url = no_blueray[0].get("href")
                logger.debug(f"'{movie.dvdid}': 存在{match_count}个同番号搜索结果，已自动选择封面比例正确的一个: {new_url}")
            else:
                # 两个结果中没有谁是蓝光影片，说明影片番号重复了
                raise MovieDuplicateError(__name__, movie.dvdid, match_count, pre_choose_urls)
        else:
            # 存在不同影片但是番号相同的情况，如MIDV-010
            raise MovieDuplicateError(__name__, movie.dvdid, match_count, pre_choose_urls)
        # 重新抓取网页
        html = request.get_html(new_url)
    container = xpath_first(html, XP["rightcolumn"], label=_SITE)
    title_tag = container.xpath(XP["title"])
    title = title_tag[0]
    cover = xpath_first(container, XP["cover"], label=_SITE)
    info = xpath_first(container, XP["video_info"], label=_SITE)
    dvdid = xpath_first(info, XP["dvdid"], label=_SITE)
    publish_date = xpath_first(info, XP["date"], label=_SITE)
    duration = xpath_first(info, XP["duration"], label=_SITE)
    director_tag = xpath_first(info, XP["director"], required=False, label=_SITE)
    if director_tag:
        movie.director = director_tag
    producer = xpath_first(info, XP["producer"], label=_SITE)
    publisher_tag = xpath_first(info, XP["publisher"], required=False, label=_SITE)
    if publisher_tag:
        movie.publisher = publisher_tag
    score_tag = xpath_first(info, XP["score"], required=False, label=_SITE)
    if score_tag:
        movie.score = score_tag.strip("()")
    genre = info.xpath(XP["genre"])
    actress = info.xpath(XP["actress"])

    movie.dvdid = dvdid
    movie.url = new_url.replace(base_url, permanent_url)
    movie.title = title.replace(dvdid, "").strip()
    if cover.startswith("//"):  # 补全URL中缺少的协议段
        cover = "https:" + cover
    movie.cover = cover
    movie.publish_date = publish_date
    movie.duration = duration
    movie.producer = producer
    movie.genre = genre
    movie.actress = actress


if __name__ == "__main__":
    base_url = permanent_url
    movie = MovieInfo("IPX-177")
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        print(e)
