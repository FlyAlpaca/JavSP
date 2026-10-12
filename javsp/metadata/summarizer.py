"""数据汇总：本地元数据整理、女优别名解析"""

import logging

from javsp.datatype import Movie, MovieInfo
from javsp.func import remove_trail_actor_in_title
from javsp.metadata.actors import ActorMapping
from javsp.workflow.models import CrawlResult

logger = logging.getLogger(__name__)

def resolve_alias(name, aliases, *, normalize_aliases=True):
    """统一展示格式，并按开关解析别名；兼容显式传入的旧字典。"""
    mapping = aliases
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
    """整理已复制的本地元数据并记录字段来源。"""
    final_info = MovieInfo(movie)
    field_sources = {}
    ########## 移除所有来源数据中，标题尾部的女优名 ##########
    if cfg.summarizer.title.remove_trailing_actor_name:
        for name, data in all_info.items():
            data.title = remove_trail_actor_in_title(data.title, data.actress)
    ########## 然后检查所有字段，如果某个字段还是默认值，则按照优先级选取数据 ##########
    # 按照优先级取出各个来源获取到的信息
    attrs = MovieInfo.get_merge_fields()
    covers, big_covers = [], []
    # 记录每个字段来自哪个来源
    for name, data in all_info.items():
        absorbed = []
        # 遍历所有属性，如果某一属性当前值为空而查询的数据中含有该属性，则采用来源的属性
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
            # 记录字段来源（仅记录首次填充该字段的来源）
            if attr in absorbed and attr not in field_sources:
                field_sources[attr] = name
        if absorbed:
            logger.debug(f"从'{name}'中获取了字段: " + " ".join(absorbed))
    # 正式番号取自本地库；关闭时保留扫描识别出的番号。
    if cfg.r18dev.respect_source_id:
        source = all_info.get("r18dev")
        if source is not None and source.title:
            if movie.dvdid:
                final_info.dvdid = source.dvdid or movie.dvdid
            else:
                final_info.cid = source.cid or movie.cid
    else:
        final_info.dvdid, final_info.cid = movie.dvdid, movie.cid

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
            raise ValueError("演员映射未初始化，请显式传入演员映射")

        def actress_name(name):
            return resolve_alias(name, aliases, normalize_aliases=cfg.r18dev.normalize_actress_name)

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
    missing_keys = [attr for attr in cfg.r18dev.required_keys if not getattr(final_info, attr, None)]
    if missing_keys:
        logger.error(f"本地记录缺少必需字段: {missing_keys}，整理失败")
        return missing_keys
    final_info.field_sources = field_sources
    for name, data in all_info.items():
        for url in (data.cover, data.big_cover):
            if url:
                final_info._cover_sources.setdefault(url, name)
    for field in ("cover", "big_cover"):
        value = getattr(final_info, field)
        for name, data in all_info.items():
            if value and getattr(data, field) == value:
                field_sources[field] = name
                break
    return final_info


class MetadataMerger:
    def __init__(self, cfg, aliases=None, *, runtime=None):
        self.cfg = cfg
        self.aliases = aliases if aliases is not None else (runtime.actor_mapping if runtime else None)

    def merge(self, movie: Movie, result: CrawlResult) -> MovieInfo:
        result.require_data()
        data = {name: info.model_copy(deep=True) for name, info in result.data.items()}
        info = _merge_info(movie, data, self.cfg, self.aliases)
        if isinstance(info, list):
            raise ValueError(f"汇总数据失败：必需字段缺失 ({info})")
        return info
