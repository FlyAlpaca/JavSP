"""数据汇总：多源数据合并、女优别名解析"""

import json
import logging

from javsp.config import Cfg, UseJavDBCover
from javsp.datatype import Movie, MovieInfo
from javsp.func import remove_trail_actor_in_title
from javsp.lib import resource_path

logger = logging.getLogger(__name__)

# 女优别名映射表（全局状态，由 load_actress_aliases 加载）
actressAliasMap: dict = {}


def load_actress_aliases():
    """加载女优别名映射表"""
    global actressAliasMap
    if Cfg().crawler.normalize_actress_name:
        actressAliasFilePath = resource_path("data/actress_alias.json")
        with open(actressAliasFilePath, encoding="utf-8") as file:
            actressAliasMap = json.load(file)


def resolve_alias(name):
    """将别名解析为固定的名字"""
    for fixedName, aliases in actressAliasMap.items():
        if name in aliases:
            return fixedName
    return name  # 如果找不到别名对应的固定名字，则返回原名


def info_summary(movie: Movie, all_info: dict[str, MovieInfo]):
    """汇总多个来源的在线数据生成最终数据"""
    final_info = MovieInfo(movie)
    ########## 部分字段配置了专门的选取逻辑，先处理这些字段 ##########
    # genre
    if "javdb" in all_info and all_info["javdb"].genre:
        final_info.genre = all_info["javdb"].genre

    ########## 移除所有抓取器数据中，标题尾部的女优名 ##########
    if Cfg().summarizer.title.remove_trailing_actor_name:
        for name, data in all_info.items():
            data.title = remove_trail_actor_in_title(data.title, data.actress)
    ########## 然后检查所有字段，如果某个字段还是默认值，则按照优先级选取数据 ##########
    # parser直接更新了all_info中的项目，而初始all_info是按照优先级生成的，已经符合配置的优先级顺序了
    # 按照优先级取出各个爬虫获取到的信息
    attrs = MovieInfo.get_merge_fields()
    covers, big_covers = [], []
    for name, data in all_info.items():
        absorbed = []
        # 遍历所有属性，如果某一属性当前值为空而爬取的数据中含有该属性，则采用爬虫的属性
        for attr in attrs:
            incoming = getattr(data, attr)
            current = getattr(final_info, attr)
            if attr == "cover":
                if incoming and (incoming not in covers):
                    covers.append(incoming)
                    absorbed.append(attr)
            elif attr == "big_cover":
                if incoming and (incoming not in big_covers):
                    big_covers.append(incoming)
                    absorbed.append(attr)
            elif attr == "uncensored":
                if (current is None) and (incoming is not None):
                    setattr(final_info, attr, incoming)
                    absorbed.append(attr)
            else:
                if (not current) and (incoming):
                    setattr(final_info, attr, incoming)
                    absorbed.append(attr)
        if absorbed:
            logger.debug(f"从'{name}'中获取了字段: " + " ".join(absorbed))
    # 使用网站的番号作为番号
    if Cfg().crawler.respect_site_avid:
        id_weight = {}
        for name, data in all_info.items():
            if data.title:
                if movie.dvdid:
                    id_weight.setdefault(data.dvdid, []).append(name)
                else:
                    id_weight.setdefault(data.cid, []).append(name)
        # 根据权重选择最终番号
        if id_weight:
            id_weight = {k: v for k, v in sorted(id_weight.items(), key=lambda x: len(x[1]), reverse=True)}
            final_id = list(id_weight.keys())[0]
            if movie.dvdid:
                final_info.dvdid = final_id
            else:
                final_info.cid = final_id
    # javdb封面有水印，优先采用其他站点的封面
    javdb_cover = getattr(all_info.get("javdb"), "cover", None)
    if javdb_cover is not None:
        match Cfg().crawler.use_javdb_cover:
            case UseJavDBCover.fallback:
                covers.remove(javdb_cover)
                covers.append(javdb_cover)
            case UseJavDBCover.no:
                covers.remove(javdb_cover)

    final_info.covers = covers
    final_info.big_covers = big_covers
    # 对cover和big_cover赋值，避免后续检查必须字段时出错
    if covers:
        final_info.cover = covers[0]
    if big_covers:
        final_info.big_cover = big_covers[0]
    ########## 部分字段放在最后进行检查 ##########
    # 特殊的 genre
    if final_info.genre is None:
        final_info.genre = []
    if movie.hard_sub:
        final_info.genre.append("内嵌字幕")
    if movie.uncensored:
        final_info.genre.append("无码流出/破解")

    # 女优别名固定
    if Cfg().crawler.normalize_actress_name and bool(final_info.actress_pics):
        final_info.actress = [resolve_alias(i) for i in final_info.actress]
        if final_info.actress_pics:
            final_info.actress_pics = {resolve_alias(key): value for key, value in final_info.actress_pics.items()}

    # 检查是否所有必需的字段都已经获得了值
    missing_keys = [attr for attr in Cfg().crawler.required_keys if not getattr(final_info, attr, None)]
    if missing_keys:
        logger.error(f"所有抓取器均未获取到字段: {missing_keys}，抓取失败")
        return missing_keys
    # 必需字段均已获得了值：将最终的数据附加到movie
    movie.info = final_info
    return None
