"""封面与剧照下载、海报裁剪及标签生成。"""

import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from PIL import Image

from javsp.concurrency import finish_executor
from javsp.cropper import get_cropper
from javsp.datatype import Movie
from javsp.lib import resource_path
from javsp.publish.image import LabelPosition, add_label_to_poster, get_pic_size, valid_pic
from javsp.scan.file import get_fmt_size
from javsp.web.base import download

logger = logging.getLogger(__name__)

# 海报标签图片（模块级常量）
SUBTITLE_MARK_FILE = Image.open(os.path.abspath(resource_path("image/sub_mark.png")))
UNCENSORED_MARK_FILE = Image.open(os.path.abspath(resource_path("image/unc_mark.png")))


def download_cover(covers, fanart_path, big_covers=None, movie_id="", *, cfg):
    """每组最多并发下载三张封面，高清全部失败后才尝试普通封面。"""
    failed_reasons = []
    retry = cfg.network.retry
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
                        info = download(url, temp_path, network=cfg.network)
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
        futures = []
        try:
            for url in urls:
                futures.append(executor.submit(fetch, url))
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
            finish_executor(executor, futures)
        return None

    result = download_group(big_covers or [], highres=True)
    if result is None:
        result = download_group(covers, highres=False)
    if result is not None:
        return result
    reason_detail = "; ".join(failed_reasons) if failed_reasons else "无可用封面地址"
    logger.error(f"下载封面图片失败: {movie_id}: {reason_detail}")
    return None, reason_detail


def download_extra_fanarts(movie: Movie, scrape_interval: float, tqdm_bar=None, destination_dir=None, *, cfg):
    """并发保存全部剧照，按统一的启动间隔限速，等待所有任务结束。"""
    warnings = []
    if not movie.info.preview_pics:
        return warnings
    destination_dir = destination_dir or os.path.join(movie.save_dir, "extrafanart")
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
            info = download(url, temp_path, network=cfg.network)
            if not valid_pic(temp_path):
                warning = f"下载剧照{idx}失败: {url}: 图片无效或已损坏"
                warnings.append(warning)
                logger.warning(warning)
                return
            filesize = get_fmt_size(temp_path)
            width, height = get_pic_size(temp_path)
            elapsed = time.strftime("%M:%S", time.gmtime(info["elapsed"]))
            speed = get_fmt_size(info["rate"]) + "/s"
            pic_path = get_pic_path(os.path.join(destination_dir, str(idx)), temp_path)
            os.replace(temp_path, pic_path)
            logger.info(f"已下载剧照{url} {os.path.basename(pic_path)}: {width}x{height}, {filesize} [{elapsed}, {speed}]")
        except Exception as e:
            warning = f"下载剧照{idx}失败: {url}, {e}"
            warnings.append(warning)
            logger.warning(warning)
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="fanart")
    futures = []
    try:
        for idx, url in enumerate(movie.info.preview_pics):
            futures.append(executor.submit(fetch, idx, url))
        for completed, future in enumerate(as_completed(futures), start=1):
            future.result()
            if tqdm_bar is not None:
                tqdm_bar.set_description(f"下载剧照 {completed}/{len(futures)}")
    finally:
        finish_executor(executor, futures)

    return warnings


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


def process_poster(movie: Movie, cfg, folder_cover_path=None, generate_folder_cover=True):
    """处理封面图片：裁剪、添加标签，并生成文件夹封面缩略图"""


    def should_use_ai_crop_match(label):
        for r in cfg.summarizer.cover.crop.on_id_pattern:
            if re.match(r, label):
                return True
        return False

    crop_engine = None
    if movie.info.uncensored or movie.data_src == "fc2" or should_use_ai_crop_match(movie.info.label.upper()):
        crop_engine = cfg.summarizer.cover.crop.engine
    cropper = get_cropper(crop_engine)
    with Image.open(movie.fanart_file) as fanart_image:
        fanart_cropped = cropper.crop(fanart_image)

    if cfg.summarizer.cover.add_label:
        if movie.hard_sub:
            fanart_cropped = add_label_to_poster(fanart_cropped, SUBTITLE_MARK_FILE, LabelPosition.BOTTOM_RIGHT)
        if movie.uncensored:
            fanart_cropped = add_label_to_poster(fanart_cropped, UNCENSORED_MARK_FILE, LabelPosition.BOTTOM_LEFT)
    fanart_cropped.save(movie.poster_file)

    folder_cover = folder_cover_path or os.path.join(movie.save_dir, "folder.jpg")
    # 自定义命名已使用 folder.jpg 时保留原图，避免缩略图覆盖海报或横版封面。
    image_paths = {os.path.abspath(movie.poster_file), os.path.abspath(movie.fanart_file)}
    if generate_folder_cover and os.path.abspath(folder_cover) not in image_paths:
        with fanart_cropped.convert("RGB") as thumbnail:
            thumbnail.thumbnail((300, 450), Image.Resampling.LANCZOS)
            thumbnail.save(folder_cover, format="JPEG")
