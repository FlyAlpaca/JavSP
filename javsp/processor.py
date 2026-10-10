"""文件处理：命名、封面下载、海报处理"""

import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from PIL import Image

from javsp.config import Cfg
from javsp.cropper import get_cropper
from javsp.datatype import Movie
from javsp.file import get_fmt_size, get_remaining_path_len, replace_illegal_chars
from javsp.func import split_by_punc
from javsp.image import LabelPosition, add_label_to_poster, get_pic_size, valid_pic
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
        # remaining 为负数时表示需要削减的字符数，确保截断后长度不为负
        copyd["title"] = copyd["title"][: max(0, len(copyd["title"]) + remaining)]
        copyd["rawtitle"] = copyd["rawtitle"][: max(0, len(copyd["rawtitle"]) + remaining)]
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

        # 截断后仍超长则给出明确警告，后续文件操作会暴露实际问题
        long_path = os.path.join(save_dir, basename + longest_ext)
        if get_remaining_path_len(os.path.abspath(long_path)) <= 0:
            logger.warning(f"生成的整理路径仍超过长度限制，请检查命名模板或输出目录: {long_path}")

        return legalize_info()


def download_cover(covers, fanart_path, big_covers=None, movie_id=""):
    """每组最多并发下载三张封面，高清全部失败后才尝试普通封面。"""
    failed_reasons = []
    retry = Cfg().network.retry
    temp_dir = os.path.dirname(os.path.abspath(fanart_path))

    def download_group(urls, highres):
        urls = list(dict.fromkeys(urls))
        if not urls:
            return None
        stopped = threading.Event()
        winner_lock = threading.Lock()
        label = "高清封面" if highres else "封面"

        def fetch(url):
            temp_path = None
            try:
                if stopped.is_set():
                    return None
                with tempfile.NamedTemporaryFile(prefix=".javsp-cover-", dir=temp_dir, delete=False) as temp:
                    temp_path = temp.name
                for attempt in range(retry):
                    if stopped.is_set():
                        return None
                    try:
                        info = download(url, temp_path)
                        if stopped.is_set():
                            return None
                        if not valid_pic(temp_path):
                            failed_reasons.append(f"{label} {url}: 图片无效或已损坏")
                            return None
                        detail = ""
                        if highres:
                            filesize = get_fmt_size(temp_path)
                            width, height = get_pic_size(temp_path)
                            elapsed = time.strftime("%M:%S", time.gmtime(info["elapsed"]))
                            speed = get_fmt_size(info["rate"]) + "/s"
                            detail = f"{width}x{height}, {filesize} [{elapsed}, {speed}]"
                        with winner_lock:
                            if stopped.is_set():
                                return None
                            pic_path = get_pic_path(fanart_path, temp_path)
                            os.replace(temp_path, pic_path)
                            stopped.set()
                        return url, pic_path, detail
                    except requests.exceptions.HTTPError as e:
                        # 高清地址可能由规则猜测生成，HTTP错误不再重试。
                        if highres:
                            status = e.response.status_code if e.response is not None else "?"
                            failed_reasons.append(f"{label} {url}: HTTP {status}")
                            return None
                        if attempt == retry - 1:
                            failed_reasons.append(f"{label} {url}: {type(e).__name__}: {e} (重试{retry}次)")
                    except Exception as e:
                        if attempt == retry - 1:
                            failed_reasons.append(f"{label} {url}: {type(e).__name__}: {e} (重试{retry}次)")
                return None
            except Exception as e:
                failed_reasons.append(f"{label} {url}: {type(e).__name__}: {e}")
                return None
            finally:
                if temp_path is not None:
                    try:
                        os.unlink(temp_path)
                    except FileNotFoundError:
                        pass

        executor = ThreadPoolExecutor(max_workers=min(3, len(urls)), thread_name_prefix="cover")
        try:
            futures = [executor.submit(fetch, url) for url in urls]
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    url, pic_path, detail = result
                    if highres:
                        logger.info(f"已下载高清封面: {detail}")
                    else:
                        logger.debug(f"已下载封面: '{url}'")
                    return url, pic_path
        finally:
            stopped.set()
            # 回收在途请求和临时文件后再返回，避免批量运行时后台线程累积。
            executor.shutdown(wait=True, cancel_futures=True)
        return None

    result = download_group(big_covers or [], highres=True)
    if result is None:
        result = download_group(covers, highres=False)
    if result is not None:
        return result
    reason_detail = "; ".join(failed_reasons) if failed_reasons else "无可用封面地址"
    logger.error(f"下载封面图片失败: {movie_id}: {reason_detail}")
    return None, reason_detail


def download_extra_fanarts(movie: Movie, scrape_interval: float, tqdm_bar=None):
    """并发保存全部剧照，按统一的启动间隔限速，等待所有任务结束。"""
    if not movie.info.preview_pics:
        return
    destination_dir = os.path.join(movie.save_dir, "extrafanart")
    os.makedirs(destination_dir, exist_ok=True)
    start_lock = threading.Lock()
    next_start = 0.0

    def fetch(idx, url):
        nonlocal next_start
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".javsp-fanart-", dir=destination_dir, delete=False) as temp:
                temp_path = temp.name
            with start_lock:
                delay = next_start - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                next_start = time.monotonic() + max(0, scrape_interval)
            info = download(url, temp_path)
            if not valid_pic(temp_path):
                logger.warning(f"下载剧照{idx}失败: {url}: 图片无效或已损坏")
                return
            filesize = get_fmt_size(temp_path)
            width, height = get_pic_size(temp_path)
            elapsed = time.strftime("%M:%S", time.gmtime(info["elapsed"]))
            speed = get_fmt_size(info["rate"]) + "/s"
            pic_path = get_pic_path(os.path.join(destination_dir, str(idx)), temp_path)
            os.replace(temp_path, pic_path)
            logger.info(f"已下载剧照{url} {os.path.basename(pic_path)}: {width}x{height}, {filesize} [{elapsed}, {speed}]")
        except Exception as e:
            logger.warning(f"下载剧照{idx}失败: {url}, {e}")
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="fanart") as executor:
        futures = [executor.submit(fetch, idx, url) for idx, url in enumerate(movie.info.preview_pics)]
        for completed, future in enumerate(as_completed(futures), start=1):
            future.result()
            if tqdm_bar is not None:
                tqdm_bar.set_description(f"下载剧照 {completed}/{len(futures)}")


def get_pic_path(fanart_path, image_path):
    """根据下载图片的实际格式生成路径，保留原始图片内容。"""
    fanart_base = os.path.splitext(fanart_path)[0]
    with Image.open(image_path) as image:
        image_format = image.format
    # 同一格式有多个后缀时使用常见名称，其他格式使用 Pillow 注册的后缀。
    extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "TIFF": ".tiff"}.get(image_format)
    if extension is None:
        extension = next((ext for ext, fmt in Image.registered_extensions().items() if fmt == image_format), None)
    if extension is None:
        raise ValueError(f"图片格式没有对应扩展名: {image_format}")
    return fanart_base + extension


def process_poster(movie: Movie):
    """处理封面图片：裁剪、添加标签，并生成文件夹封面缩略图"""

    def should_use_ai_crop_match(label):
        for r in Cfg().summarizer.cover.crop.on_id_pattern:
            if re.match(r, label):
                return True
        return False

    crop_engine = None
    if movie.info.uncensored or movie.data_src == "fc2" or should_use_ai_crop_match(movie.info.label.upper()):
        crop_engine = Cfg().summarizer.cover.crop.engine
    cropper = get_cropper(crop_engine)
    with Image.open(movie.fanart_file) as fanart_image:
        fanart_cropped = cropper.crop(fanart_image)

    if Cfg().summarizer.cover.add_label:
        if movie.hard_sub:
            fanart_cropped = add_label_to_poster(fanart_cropped, SUBTITLE_MARK_FILE, LabelPosition.BOTTOM_RIGHT)
        if movie.uncensored:
            fanart_cropped = add_label_to_poster(fanart_cropped, UNCENSORED_MARK_FILE, LabelPosition.BOTTOM_LEFT)
    fanart_cropped.save(movie.poster_file)

    folder_cover = os.path.join(movie.save_dir, "folder.jpg")
    # 自定义命名已使用 folder.jpg 时保留原图，避免缩略图覆盖海报或横版封面。
    if os.path.abspath(folder_cover) not in {os.path.abspath(movie.poster_file), os.path.abspath(movie.fanart_file)}:
        with fanart_cropped.convert("RGB") as thumbnail:
            thumbnail.thumbnail((300, 450), Image.Resampling.LANCZOS)
            thumbnail.save(folder_cover, format="JPEG")
