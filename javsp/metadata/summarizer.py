"""数据汇总：多源数据合并、女优别名解析"""

import logging
from functools import lru_cache

from javsp.config import Cfg, UseJavDBCover
from javsp.datatype import GenreMap, Movie, MovieInfo
from javsp.func import remove_trail_actor_in_title
from javsp.metadata.actors import ActorMapping, prepare_actor_mapping
from javsp.workflow.models import CrawlResult

logger = logging.getLogger(__name__)

# 女优别名映射表（全局状态，由 load_actress_aliases 加载）
actressAliasMap: ActorMapping | dict | None = None


def load_actress_aliases(cfg=None):
    """启动时更新 GitHub 映射，格式化不受别名开关影响。"""
    global actressAliasMap
    cfg = cfg if cfg is not None else Cfg()
    mapping = prepare_actor_mapping(cfg._actor_mapping_path, proxy=cfg.network.proxy_server)
    cfg._actor_mapping = mapping
    actressAliasMap = mapping
    return mapping


def resolve_alias(name, aliases=None, *, normalize_aliases=True):
    """统一展示格式，并按开关解析别名；兼容显式传入的旧字典。"""
    mapping = actressAliasMap if aliases is None else aliases
    if isinstance(mapping, ActorMapping):
        return mapping.resolve(name, normalize_aliases=normalize_aliases)
    if mapping is None:
        raise ValueError("演员映射未初始化，请先完成启动准备")
    if not normalize_aliases:
        return name
    for fixedName, names in mapping.items():
        if name in names:
            return fixedName
    return name  # 如果找不到别名对应的固定名字，则返回原名


def _merge_info(movie: Movie, all_info: dict[str, MovieInfo], cfg, aliases):
    """按来源优先级汇总数据。"""
    final_info = MovieInfo(movie)
    ########## 部分字段配置了专门的选取逻辑，先处理这些字段 ##########
    # genre
    field_sources = {}
    if not ("r18dev" in all_info and all_info["r18dev"].genre) and "javdb" in all_info and all_info["javdb"].genre:
        final_info.genre = list(all_info["javdb"].genre)
        field_sources["genre"] = "javdb"

    ########## 移除所有抓取器数据中，标题尾部的女优名 ##########
    if cfg.summarizer.title.remove_trailing_actor_name:
        for name, data in all_info.items():
            data.title = remove_trail_actor_in_title(data.title, data.actress)
    ########## 然后检查所有字段，如果某个字段还是默认值，则按照优先级选取数据 ##########
    # parser直接更新了all_info中的项目，而初始all_info是按照优先级生成的，已经符合配置的优先级顺序了
    # 按照优先级取出各个爬虫获取到的信息
    attrs = MovieInfo.get_merge_fields()
    covers, big_covers = [], []
    # 记录每个字段来自哪个爬虫
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
            # 记录字段来源（仅记录首次填充该字段的爬虫）
            if attr in absorbed and attr not in field_sources:
                field_sources[attr] = name
        if absorbed:
            logger.debug(f"从'{name}'中获取了字段: " + " ".join(absorbed))
    # 使用网站的番号作为番号
    if cfg.crawler.respect_site_avid:
        id_weight = {}
        for name, data in all_info.items():
            if data.title:
                if movie.dvdid:
                    id_weight.setdefault(data.dvdid, []).append(name)
                else:
                    id_weight.setdefault(data.cid, []).append(name)
        # 根据权重选择最终番号
        if "r18dev" in all_info and all_info["r18dev"].title:
            final_id = all_info["r18dev"].dvdid if movie.dvdid else all_info["r18dev"].cid
            if movie.dvdid:
                final_info.dvdid = final_id or movie.dvdid
            else:
                final_info.cid = final_id or movie.cid
        elif id_weight:
            id_weight = {k: v for k, v in sorted(id_weight.items(), key=lambda x: len(x[1]), reverse=True)}
            final_id = list(id_weight.keys())[0]
            original_id = movie.dvdid or movie.cid
            if final_id and final_id != original_id:
                logger.debug(f"番号更正: '{original_id}' -> '{final_id}' (来源: {', '.join(id_weight[final_id])})")
            if movie.dvdid:
                final_info.dvdid = final_id
            else:
                final_info.cid = final_id
    else:
        logger.debug(f"respect_site_avid=False, 保留文件名识别的番号: '{movie.dvdid or movie.cid}'")
    # javdb封面有水印，优先采用其他站点的封面
    javdb_cover = getattr(all_info.get("javdb"), "cover", None)
    if javdb_cover is not None:
        match cfg.crawler.use_javdb_cover:
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

    # 名称格式始终统一；旧开关仅控制是否将 keyword 别名归到主名。
    if final_info.actress or final_info.actress_pics:
        if aliases is None:
            aliases = cfg._actor_mapping
            if aliases is None:
                # 独立组件调用可读取缓存；联网更新由启动流程负责。
                aliases = ActorMapping.from_file(cfg._actor_mapping_path)
                cfg._actor_mapping = aliases

        def actress_name(name):
            return resolve_alias(name, aliases, normalize_aliases=cfg.crawler.normalize_actress_name)

        if final_info.actress:
            final_info.actress = list(dict.fromkeys(actress_name(name) for name in final_info.actress))
        if final_info.actress_pics:
            pictures = {}
            for name, url in final_info.actress_pics.items():
                key = actress_name(name)
                if key not in pictures or not pictures[key]:
                    pictures[key] = url
            final_info.actress_pics = pictures

    # 检查是否所有必需的字段都已经获得了值
    missing_keys = [attr for attr in cfg.crawler.required_keys if not getattr(final_info, attr, None)]
    if missing_keys:
        logger.error(f"所有抓取器均未获取到字段: {missing_keys}，抓取失败")
        return missing_keys
    final_info.field_sources = field_sources
    for name, data in all_info.items():
        for url in (data.cover, data.big_cover):
            if url:
                final_info._cover_sources.setdefault(url, name)
    # cover 的优先级可能由 javdb 策略调整。
    for field in ("cover", "big_cover"):
        value = getattr(final_info, field)
        for name, data in all_info.items():
            if value and getattr(data, field) == value:
                field_sources[field] = name
                break
    return final_info


@lru_cache(maxsize=16)
def _genre_map_for_source(name: str) -> GenreMap | None:
    """分类表随安装包发布，批量处理时只需读取一次。"""
    try:
        return GenreMap(f"data/genre_{name}.csv")
    except FileNotFoundError:
        return None


def normalize_genres(all_info: dict[str, MovieInfo]):
    """对成功来源归一化分类，已有归一化结果优先。"""
    for name, info in all_info.items():
        if info.genre_norm or not (info.genre_id or info.genre):
            continue
        genre_map = _genre_map_for_source(name)
        if genre_map is None:
            logger.debug(f"'{name}' 缺少 genre 映射表，跳过归一化")
            continue
        info.genre_norm = genre_map.map(info.genre_id or info.genre)


class MetadataMerger:
    def __init__(self, cfg, aliases=None):
        self.cfg = cfg
        self.aliases = aliases if aliases is not None else cfg._actor_mapping

    def merge(self, movie: Movie, result: CrawlResult) -> MovieInfo:
        result.require_data()
        data = {name: info.model_copy(deep=True) for name, info in result.data.items()}
        normalize_genres(data)
        info = _merge_info(movie, data, self.cfg, self.aliases)
        if isinstance(info, list):
            raise ValueError(f"汇总数据失败：必需字段缺失 ({info})")
        return info


def info_summary(movie: Movie, all_info: dict[str, MovieInfo]):
    """兼容旧接口：缺失字段时返回列表，成功时更新 movie.info。"""
    data = {name: info.model_copy(deep=True) for name, info in all_info.items()}
    normalize_genres(data)
    info = _merge_info(movie, data, Cfg(), actressAliasMap)
    if isinstance(info, list):
        return info
    movie.info = info
    return None
