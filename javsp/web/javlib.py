"""从JavLibrary抓取数据"""

import logging
from urllib.parse import urlsplit

from javsp.config import Cfg, CrawlerID
from javsp.datatype import MovieInfo
from javsp.web.base import Request, read_proxy, resp2html
from javsp.web.exceptions import CrawlerError, MovieDuplicateError, MovieNotFoundError
from javsp.web.proxyfree import get_proxy_free_url

# 初始化Request实例
request = Request(use_scraper=True)

logger = logging.getLogger(__name__)
permanent_url = "https://www.javlibrary.com"
base_url = ""


def init_network_cfg():
    """设置合适的代理模式和base_url

    优先级：免代理地址（无代理访问）> 永久域名（代理访问）
    """
    request.timeout = 5
    # 1. 优先尝试免代理地址（config配置 + 自动获取）
    proxy_free_url = get_proxy_free_url("javlib", str(Cfg().network.proxy_free[CrawlerID.javlib]))
    if proxy_free_url:
        request.proxies = {}
        try:
            resp = request.get(proxy_free_url, delay_raise=True)
            if resp.status_code == 200:
                request.timeout = Cfg().network.timeout.total_seconds()
                return proxy_free_url
        except Exception as e:
            logger.debug(f"免代理地址不可用: {proxy_free_url}: {e}")
    # 2. 回退到代理访问永久域名
    if Cfg().network.proxy_server:
        request.proxies = read_proxy()
        try:
            resp = request.get(permanent_url, delay_raise=True)
            if resp.status_code == 200:
                request.timeout = Cfg().network.timeout.total_seconds()
                return permanent_url
        except Exception as e:
            logger.debug(f"代理访问永久域名失败: {e}")
    logger.warning("无法绕开JavLib的反爬机制")
    request.timeout = Cfg().network.timeout.total_seconds()
    return permanent_url


# TODO: 发现JavLibrary支持使用cid搜索，会直接跳转到对应的影片页面，也许可以利用这个功能来做cid到dvdid的转换
def parse_data(movie: MovieInfo):
    """解析指定番号的影片数据"""
    global base_url
    if not base_url:
        base_url = init_network_cfg()
        logger.debug(f"JavLib网络配置: {base_url}, proxy={request.proxies}")
    url = new_url = f"{base_url}/cn/vl_searchbyid.php?keyword={movie.dvdid}"
    resp = request.get(url)
    html = resp2html(resp)
    if resp.history:
        if urlsplit(resp.url).netloc == urlsplit(base_url).netloc:
            # 出现301重定向通常且新老地址netloc相同时，说明搜索到了影片且只有一个结果
            new_url = resp.url
        else:
            # 重定向到了不同的netloc时，新地址并不是影片地址。这种情况下新地址中丢失了path字段，
            # 为无效地址（应该是JavBus重定向配置有问题），需要使用新的base_url抓取数据
            base_url = "https://" + urlsplit(resp.url).netloc
            logger.warning(f"请将配置文件中的JavLib免代理地址更新为: {base_url}")
            return parse_data(movie)
    else:  # 如果有多个搜索结果则不会自动跳转，此时需要程序介入选择搜索结果
        video_tags = html.xpath("//div[@class='video'][@id]/a")
        # 通常第一部影片就是我们要找的，但是以免万一还是遍历所有搜索结果
        pre_choose = []
        for tag in video_tags:
            tag_dvdid = tag.xpath("div[@class='id']/text()")[0]
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
    container = html.xpath("/html/body/div/div[@id='rightcolumn']")[0]
    title_tag = container.xpath("div/h3/a/text()")
    title = title_tag[0]
    cover = container.xpath("//img[@id='video_jacket_img']/@src")[0]
    info = container.xpath("//div[@id='video_info']")[0]
    dvdid = info.xpath("div[@id='video_id']//td[@class='text']/text()")[0]
    publish_date = info.xpath("div[@id='video_date']//td[@class='text']/text()")[0]
    duration = info.xpath("div[@id='video_length']//span[@class='text']/text()")[0]
    director_tag = info.xpath("//span[@class='director']/a/text()")
    if director_tag:
        movie.director = director_tag[0]
    producer = info.xpath("//span[@class='maker']/a/text()")[0]
    publisher_tag = info.xpath("//span[@class='label']/a/text()")
    if publisher_tag:
        movie.publisher = publisher_tag[0]
    score_tag = info.xpath("//span[@class='score']/text()")
    if score_tag:
        movie.score = score_tag[0].strip("()")
    genre = info.xpath("//span[@class='genre']/a/text()")
    actress = info.xpath("//span[@class='star']/a/text()")

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
    import pretty_errors

    pretty_errors.configure(display_link=True)
    base_url = permanent_url
    movie = MovieInfo("IPX-177")
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        print(e)
