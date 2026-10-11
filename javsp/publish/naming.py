"""仅计算输出命名，不执行文件写入。"""

import logging
import os
from pathlib import Path

from javsp.datatype import Movie
from javsp.func import split_by_punc
from javsp.scan.file import get_remaining_path_len, replace_illegal_chars

logger = logging.getLogger(__name__)


def generate_names(movie: Movie, cfg, *, base_dir=None):
    """按照模板生成相关文件的文件名"""

    def legalize_path(path: str):
        """Windows下文件名中不能包含换行 #467"""
        return "".join(c for c in path if c not in {"\n"})

    base_dir = Path(base_dir or Path.cwd()).resolve()
    info = movie.info
    # 准备用来填充命名模板的字典
    d = info.get_info_dic(cfg)

    if info.actress and len(info.actress) > cfg.summarizer.path.max_actress_count:
        logging.debug("女优人数过多，按配置保留了其中的前n个: " + ",".join(info.actress))
        actress = info.actress[: cfg.summarizer.path.max_actress_count] + ["…"]
    else:
        actress = info.actress
    d["actress"] = ",".join(actress) if actress else cfg.summarizer.default.actress

    # 保存label供后面判断裁剪图片的方式使用
    info.label = d["label"].upper()
    # 处理字段：替换不能作为文件名的字符，移除首尾的空字符
    for k, v in d.items():
        d[k] = replace_illegal_chars(v.strip())

    # 生成nfo文件中的影片标题
    nfo_title = cfg.summarizer.nfo.title_pattern.format(**d)
    info.nfo_title = nfo_title

    # 使用字典填充模板，生成相关文件的路径（多分片影片要考虑CD-x部分）
    if info.title_break is not None:
        title_break = info.title_break
    else:
        title_break = split_by_punc(d["title"])
    if info.ori_title_break is not None:
        ori_title_break = info.ori_title_break
    else:
        ori_title_break = split_by_punc(d["rawtitle"])
    copyd = d.copy()

    def legalize_info():
        if movie.save_dir is not None:
            movie.save_dir = legalize_path(movie.save_dir)
        if movie.nfo_file is not None:
            movie.nfo_file = legalize_path(movie.nfo_file)
        if movie.fanart_file is not None:
            movie.fanart_file = legalize_path(movie.fanart_file)
        if movie.poster_file is not None:
            movie.poster_file = legalize_path(movie.poster_file)
        if d["title"] != copyd["title"]:
            logger.info(f"自动截短标题为:\n{copyd['title']}")
        if d["rawtitle"] != copyd["rawtitle"]:
            logger.info(f"自动截短原始标题为:\n{copyd['rawtitle']}")
        return

    copyd["num"] = copyd["num"] + movie.attr_str
    longest_ext = max((os.path.splitext(i)[1] for i in movie.files), key=len)
    for end in range(len(ori_title_break), 0, -1):
        copyd["rawtitle"] = replace_illegal_chars("".join(ori_title_break[:end]).strip())
        for sub_end in range(len(title_break), 0, -1):
            copyd["title"] = replace_illegal_chars("".join(title_break[:sub_end]).strip())
            if cfg.summarizer.move_files:
                save_dir = os.path.normpath(str(base_dir / cfg.summarizer.path.output_folder_pattern.format(**copyd))).strip()
                basename = os.path.normpath(cfg.summarizer.path.basename_pattern.format(**copyd)).strip()
            else:
                # 如果不整理文件，则保存抓取的数据到当前目录
                save_dir = os.path.dirname(movie.files[0])
                filebasename = os.path.basename(movie.files[0])
                ext = os.path.splitext(filebasename)[1]
                basename = filebasename.replace(ext, "")
            long_path = os.path.join(save_dir, basename + longest_ext)
            remaining = get_remaining_path_len(os.path.abspath(long_path), cfg)
            if remaining > 0:
                movie.save_dir = save_dir
                movie.basename = basename
                movie.nfo_file = os.path.join(
                    save_dir,
                    cfg.summarizer.nfo.basename_pattern.format(**copyd) + ".nfo",
                )
                movie.fanart_file = os.path.join(
                    save_dir,
                    cfg.summarizer.fanart.basename_pattern.format(**copyd) + ".jpg",
                )
                movie.poster_file = os.path.join(
                    save_dir,
                    cfg.summarizer.cover.basename_pattern.format(**copyd) + ".jpg",
                )
                return legalize_info()
    else:
        # 以防万一，当整理路径非常深或者标题起始很长一段没有标点符号时，硬性截短生成的名称
        # remaining 为负数时表示需要削减的字符数，确保截断后长度不为负
        copyd["title"] = copyd["title"][: max(0, len(copyd["title"]) + remaining)]
        copyd["rawtitle"] = copyd["rawtitle"][: max(0, len(copyd["rawtitle"]) + remaining)]
        # 如果不整理文件，则保存抓取的数据到当前目录
        if not cfg.summarizer.move_files:
            save_dir = os.path.dirname(movie.files[0])
            filebasename = os.path.basename(movie.files[0])
            ext = os.path.splitext(filebasename)[1]
            basename = filebasename.replace(ext, "")
        else:
            save_dir = os.path.normpath(str(base_dir / cfg.summarizer.path.output_folder_pattern.format(**copyd))).strip()
            basename = os.path.normpath(cfg.summarizer.path.basename_pattern.format(**copyd)).strip()
        movie.save_dir = save_dir
        movie.basename = basename

        movie.nfo_file = os.path.join(save_dir, cfg.summarizer.nfo.basename_pattern.format(**copyd) + ".nfo")
        movie.fanart_file = os.path.join(save_dir, cfg.summarizer.fanart.basename_pattern.format(**copyd) + ".jpg")
        movie.poster_file = os.path.join(save_dir, cfg.summarizer.cover.basename_pattern.format(**copyd) + ".jpg")

        # 截断后仍超长则给出明确警告，后续文件操作会暴露实际问题
        long_path = os.path.join(save_dir, basename + longest_ext)
        if get_remaining_path_len(os.path.abspath(long_path), cfg) <= 0:
            logger.warning(f"生成的整理路径仍超过长度限制，请检查命名模板或输出目录: {long_path}")

        return legalize_info()
