"""文件处理：命名、封面下载、海报处理"""

import logging
import os
import re
import time

import requests
from PIL import Image

from javsp.config import Cfg
from javsp.cropper import get_cropper
from javsp.datatype import Movie
from javsp.file import get_fmt_size, get_remaining_path_len, replace_illegal_chars
from javsp.func import split_by_punc
from javsp.image import LabelPostion, add_label_to_poster, get_pic_size, valid_pic
from javsp.lib import resource_path
from javsp.web.base import download

logger = logging.getLogger(__name__)

# 海报标签图片（模块级常量）
SUBTITLE_MARK_FILE = Image.open(os.path.abspath(resource_path("image/sub_mark.png")))
UNCENSORED_MARK_FILE = Image.open(os.path.abspath(resource_path("image/unc_mark.png")))


def generate_names(movie: Movie):
    """按照模板生成相关文件的文件名"""

    def legalize_path(path: str):
        """Windows下文件名中不能包含换行 #467"""
        return "".join(c for c in path if c not in {"\n"})

    info = movie.info
    # 准备用来填充命名模板的字典
    d = info.get_info_dic()

    if info.actress and len(info.actress) > Cfg().summarizer.path.max_actress_count:
        logging.debug("女优人数过多，按配置保留了其中的前n个: " + ",".join(info.actress))
        actress = info.actress[: Cfg().summarizer.path.max_actress_count] + ["…"]
    else:
        actress = info.actress
    d["actress"] = ",".join(actress) if actress else Cfg().summarizer.default.actress

    # 保存label供后面判断裁剪图片的方式使用
    info.label = d["label"].upper()
    # 处理字段：替换不能作为文件名的字符，移除首尾的空字符
    for k, v in d.items():
        d[k] = replace_illegal_chars(v.strip())

    # 生成nfo文件中的影片标题
    nfo_title = Cfg().summarizer.nfo.title_pattern.format(**d)
    info.nfo_title = nfo_title

    # 使用字典填充模板，生成相关文件的路径（多分片影片要考虑CD-x部分）
    "" if len(movie.files) <= 1 else "-CD1"
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
            if Cfg().summarizer.move_files:
                save_dir = os.path.normpath(Cfg().summarizer.path.output_folder_pattern.format(**copyd)).strip()
                basename = os.path.normpath(Cfg().summarizer.path.basename_pattern.format(**copyd)).strip()
            else:
                # 如果不整理文件，则保存抓取的数据到当前目录
                save_dir = os.path.dirname(movie.files[0])
                filebasename = os.path.basename(movie.files[0])
                ext = os.path.splitext(filebasename)[1]
                basename = filebasename.replace(ext, "")
            long_path = os.path.join(save_dir, basename + longest_ext)
            remaining = get_remaining_path_len(os.path.abspath(long_path))
            if remaining > 0:
                movie.save_dir = save_dir
                movie.basename = basename
                movie.nfo_file = os.path.join(
                    save_dir,
                    Cfg().summarizer.nfo.basename_pattern.format(**copyd) + ".nfo",
                )
                movie.fanart_file = os.path.join(
                    save_dir,
                    Cfg().summarizer.fanart.basename_pattern.format(**copyd) + ".jpg",
                )
                movie.poster_file = os.path.join(
                    save_dir,
                    Cfg().summarizer.cover.basename_pattern.format(**copyd) + ".jpg",
                )
                return legalize_info()
    else:
        # 以防万一，当整理路径非常深或者标题起始很长一段没有标点符号时，硬性截短生成的名称
        copyd["title"] = copyd["title"][:remaining]
        copyd["rawtitle"] = copyd["rawtitle"][:remaining]
        # 如果不整理文件，则保存抓取的数据到当前目录
        if not Cfg().summarizer.move_files:
            save_dir = os.path.dirname(movie.files[0])
            filebasename = os.path.basename(movie.files[0])
            ext = os.path.splitext(filebasename)[1]
            basename = filebasename.replace(ext, "")
        else:
            save_dir = os.path.normpath(Cfg().summarizer.path.output_folder_pattern.format(**copyd)).strip()
            basename = os.path.normpath(Cfg().summarizer.path.basename_pattern.format(**copyd)).strip()
        movie.save_dir = save_dir
        movie.basename = basename

        movie.nfo_file = os.path.join(save_dir, Cfg().summarizer.nfo.basename_pattern.format(**copyd) + ".nfo")
        movie.fanart_file = os.path.join(save_dir, Cfg().summarizer.fanart.basename_pattern.format(**copyd) + ".jpg")
        movie.poster_file = os.path.join(save_dir, Cfg().summarizer.cover.basename_pattern.format(**copyd) + ".jpg")

        return legalize_info()


def download_cover(covers, fanart_path, big_covers=[], movie_id=""):
    """下载封面图片"""
    failed_reasons = []  # 收集失败原因
    # 优先下载高清封面
    for url in big_covers:
        pic_path = get_pic_path(fanart_path, url)
        last_err = None
        for attempt in range(Cfg().network.retry):
            try:
                info = download(url, pic_path)
                if valid_pic(pic_path):
                    filesize = get_fmt_size(pic_path)
                    width, height = get_pic_size(pic_path)
                    elapsed = time.strftime("%M:%S", time.gmtime(info["elapsed"]))
                    speed = get_fmt_size(info["rate"]) + "/s"
                    logger.info(f"已下载高清封面: {width}x{height}, {filesize} [{elapsed}, {speed}]")
                    return (url, pic_path)
                else:
                    failed_reasons.append(f"高清封面 {url}: 图片无效或已损坏")
                    break
            except requests.exceptions.HTTPError as e:
                # HTTPError通常说明猜测的高清封面地址实际不可用，因此不再重试
                failed_reasons.append(f"高清封面 {url}: HTTP {e.response.status_code if e.response else '?'}")
                break
            except Exception as e:
                last_err = e
        if last_err is not None:
            failed_reasons.append(f"高清封面 {url}: {type(last_err).__name__}: {last_err} (重试{Cfg().network.retry}次)")
    # 如果没有高清封面或高清封面下载失败
    for url in covers:
        pic_path = get_pic_path(fanart_path, url)
        last_err = None
        for attempt in range(Cfg().network.retry):
            try:
                download(url, pic_path)
                if valid_pic(pic_path):
                    logger.debug(f"已下载封面: '{url}'")
                    return (url, pic_path)
                else:
                    failed_reasons.append(f"封面 {url}: 图片无效或已损坏")
                    break
            except Exception as e:
                last_err = e
        if last_err is not None:
            failed_reasons.append(f"封面 {url}: {type(last_err).__name__}: {last_err} (重试{Cfg().network.retry}次)")
    reason_detail = "; ".join(failed_reasons) if failed_reasons else "无可用封面地址"
    logger.error(f"下载封面图片失败: {movie_id}: {reason_detail}")
    return (None, reason_detail)  # 返回 (None, 原因) 以便调用方获取详情


def get_pic_path(fanart_path, url):
    """根据 fanart 路径和封面 URL 推断图片保存路径"""
    fanart_base = os.path.splitext(fanart_path)[0]
    pic_extend = url.split(".")[-1]
    # 判断 url 是否带？后面的参数
    if "?" in pic_extend:
        pic_extend = pic_extend.split("?")[0]

    pic_path = fanart_base + "." + pic_extend
    return pic_path


def process_poster(movie: Movie):
    """处理封面图片：裁剪、添加标签"""

    def should_use_ai_crop_match(label):
        for r in Cfg().summarizer.cover.crop.on_id_pattern:
            if re.match(r, label):
                return True
        return False

    crop_engine = None
    if movie.info.uncensored or movie.data_src == "fc2" or should_use_ai_crop_match(movie.info.label.upper()):
        crop_engine = Cfg().summarizer.cover.crop.engine
    cropper = get_cropper(crop_engine)
    fanart_image = Image.open(movie.fanart_file)
    fanart_cropped = cropper.crop(fanart_image)

    if Cfg().summarizer.cover.add_label:
        if movie.hard_sub:
            fanart_cropped = add_label_to_poster(fanart_cropped, SUBTITLE_MARK_FILE, LabelPostion.BOTTOM_RIGHT)
        if movie.uncensored:
            fanart_cropped = add_label_to_poster(fanart_cropped, UNCENSORED_MARK_FILE, LabelPostion.BOTTOM_LEFT)
    fanart_cropped.save(movie.poster_file)
