"""封面与剧照下载、海报裁剪及标签生成。"""

import hashlib
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait

import requests
from PIL import Image

from javsp.concurrency import await_futures, finish_executor
from javsp.cropper import get_cropper
from javsp.datatype import Movie
from javsp.lib import resource_path
from javsp.network import download
from javsp.publish.image import LabelPosition, add_label_to_poster, get_pic_size, valid_pic
from javsp.scan.file import get_fmt_size

logger = logging.getLogger(__name__)

# 海报标签图片（模块级常量）
SUBTITLE_MARK_FILE = Image.open(os.path.abspath(resource_path("image/sub_mark.png")))
UNCENSORED_MARK_FILE = Image.open(os.path.abspath(resource_path("image/unc_mark.png")))
SUBTITLE_MARK_FILE.load()
UNCENSORED_MARK_FILE.load()

_DMM_COVER = re.compile(
    r"^https://pics\.dmm\.co\.jp/(digital/video|mono/movie/adult)/([^/]+)/([^/]+)p[sl]\.jpg$", re.I
)
_DMM_PLACEHOLDER = "efea457c7fc0fae1eb0ec93f4f719189e38b0679cac47c11b829740d9b5675d8"


def dmm_cover_candidates(covers, big_covers):
    """从已知 DMM 地址推导同一 CID 的原图、大图和缩略图，按清晰度排列。"""
    for url in [*(big_covers or []), *(covers or [])]:
        match = _DMM_COVER.fullmatch(url)
        if match and match[2].lower() == match[3].lower():
            section, cid, stem = match.groups()
            section = section.replace("mono/movie/adult", "mono/movie")
            high = f"https://awsimgsrc.dmm.co.jp/pics_dig/{section}/{cid}/{stem}pl.jpg"
            ordinary = f"https://pics.dmm.co.jp/{match[1]}/{cid}/{stem}pl.jpg"
            thumbnail = f"https://pics.dmm.co.jp/{match[1]}/{cid}/{stem}ps.jpg"
            portrait = f"https://awsimgsrc.dmm.co.jp/pics_dig/{section}/{cid}/{stem}ps.jpg"
            return high, ordinary, thumbnail, portrait
    return None


def actor_image_path(save_dir, name):
    return os.path.join(save_dir, ".actors", hashlib.sha256(name.encode("utf-8")).hexdigest()[:16] + ".jpg")


def download_actor(url, target, *, cfg, resources=None):
    """下载并校验头像；DMM 返回的 200 占位图不能作为演员照片。"""
    if resources:
        resources.check()
    download(url, target, network=cfg.network)
    if resources:
        resources.check()
    if not valid_pic(target):
        raise ValueError("头像图片无效")
    if "dmm.co.jp/" in url:
        with open(target, "rb") as image_file:
            if hashlib.file_digest(image_file, "sha256").hexdigest() == _DMM_PLACEHOLDER:
                raise ValueError("头像是 DMM 占位图")
    with Image.open(target) as image:
        image.convert("RGB").save(target, format="JPEG")


def download_cover(covers, fanart_path, big_covers=None, movie_id="", *, cfg, resources=None, optional=False):
    """使用批次图片额度（独立调用最多三线程），高清全部失败后才尝试普通封面。"""
    dmm = dmm_cover_candidates(covers, big_covers) if big_covers else None
    if dmm:
        big_covers, covers = dmm[:2], [dmm[2]]
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
            if resources:
                resources.check()
            temp_path = None
            try:
                if stopped.is_set():
                    return None
                with tempfile.NamedTemporaryFile(prefix=".javsp-cover-", dir=temp_dir, delete=False) as temp:
                    temp_path = temp.name
                for attempt in range(retry):
                    if resources:
                        resources.check()
                    if stopped.is_set():
                        return None
                    try:
                        info = download(url, temp_path, network=cfg.network)
                        if resources:
                            resources.check()
                        if stopped.is_set():
                            return None
                        if not valid_pic(temp_path):
                            failed_reasons.append(f"{label} {url}: 图片无效或已损坏")
                            return None
                        if "dmm.co.jp/" in url:
                            with open(temp_path, "rb") as image_file:
                                if hashlib.file_digest(image_file, "sha256").hexdigest() == _DMM_PLACEHOLDER:
                                    failed_reasons.append(f"{label} {url}: DMM 占位图")
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

        executor = resources.images if resources else ThreadPoolExecutor(
            max_workers=min(3, len(urls)), thread_name_prefix="cover"
        )
        futures = []
        try:
            batches = ([url] for url in urls) if dmm else [urls]
            for batch in batches:
                current = [resources.submit(executor, fetch, url) if resources else executor.submit(fetch, url)
                           for url in batch]
                futures.extend(current)
                for future in as_completed(current):
                    result = future.result()
                    if result is not None:
                        url, pic_path, detail = result
                        if highres:
                            logger.debug(f"已下载高清封面: {detail}")
                        else:
                            logger.debug(f"已下载封面: '{url}'")
                        return url, pic_path
        finally:
            stopped.set()
            # 回收在途请求和临时文件后再返回，避免批量运行时后台线程累积。
            if resources:
                for future in futures:
                    future.cancel()
                await_futures(futures)
            else:
                finish_executor(executor, futures)
        return None

    result = download_group(big_covers or [], highres=True)
    if result is None:
        result = download_group(covers, highres=False)
    if result is not None:
        return result
    reason_detail = "; ".join(failed_reasons) if failed_reasons else "无可用封面地址"
    if not optional:
        # 调用方将完整原因作为任务错误输出，避免在这里重复记录。
        logger.debug("下载封面图片失败: %s: %s", movie_id, reason_detail)
    return None, reason_detail


def download_extra_fanarts(movie: Movie, scrape_interval: float, tqdm_bar=None, destination_dir=None, *, cfg, resources=None):
    """并发保存全部剧照，按统一的启动间隔限速，等待所有任务结束。"""
    warnings = []
    if not movie.info.preview_pics:
        return warnings
    identity = getattr(movie, "dvdid", None) or getattr(movie, "cid", None) or "未知番号"
    destination_dir = destination_dir or os.path.join(movie.save_dir, "extrafanart")
    os.makedirs(destination_dir, exist_ok=True)
    start_lock = threading.Lock()
    next_start = 0.0

    def fetch(idx, url):
        if resources:
            resources.check()
        nonlocal next_start
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".javsp-fanart-", dir=destination_dir, delete=False) as temp:
                temp_path = temp.name
            if resources:
                resources.fanart_start(scrape_interval)
            else:
                with start_lock:
                    delay = next_start - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    next_start = time.monotonic() + max(0, scrape_interval)
            info = download(url, temp_path, network=cfg.network)
            if resources:
                resources.check()
            if not valid_pic(temp_path):
                warning = f"下载剧照{idx}失败: {url}: 图片无效或已损坏"
                warnings.append(warning)
                logger.debug("[%s] %s", identity, warning)
                return
            filesize = get_fmt_size(temp_path)
            width, height = get_pic_size(temp_path)
            elapsed = time.strftime("%M:%S", time.gmtime(info["elapsed"]))
            speed = get_fmt_size(info["rate"]) + "/s"
            pic_path = get_pic_path(os.path.join(destination_dir, str(idx)), temp_path)
            os.replace(temp_path, pic_path)
            logger.debug(f"已下载剧照{url} {os.path.basename(pic_path)}: {width}x{height}, {filesize} [{elapsed}, {speed}]")
        except Exception as e:
            warning = f"下载剧照{idx}失败: {url}, {e}"
            warnings.append(warning)
            logger.debug("[%s] %s", identity, warning)
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    executor = resources.images if resources else ThreadPoolExecutor(max_workers=3, thread_name_prefix="fanart")
    futures = set()
    pending = iter(enumerate(movie.info.preview_pics))
    limit = cfg.concurrency.images if resources else 3
    completed = 0
    try:
        while True:
            while len(futures) < limit:
                try:
                    idx, url = next(pending)
                except StopIteration:
                    break
                futures.add(resources.submit(executor, fetch, idx, url) if resources else executor.submit(fetch, idx, url))
            if not futures:
                break
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                future.result()
                futures.remove(future)
                completed += 1
                if tqdm_bar is not None:
                    tqdm_bar.set_description(f"下载剧照 {completed}/{len(movie.info.preview_pics)}")
    finally:
        if resources:
            for future in futures:
                future.cancel()
            await_futures(futures)
        else:
            finish_executor(executor, futures)

    if warnings:
        logger.warning("[%s] 剧照下载失败 %s/%s 张，完整原因见 DEBUG 日志", identity, len(warnings), len(movie.info.preview_pics))
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


def process_poster(movie: Movie, cfg, folder_cover_path=None, generate_folder_cover=True, portrait_path=None):
    """优先使用独立竖版原图，再添加标签并生成同清晰度文件夹封面。"""


    def should_use_ai_crop_match(label):
        for r in cfg.summarizer.cover.crop.on_id_pattern:
            if re.match(r, label):
                return True
        return False

    crop_engine = None
    if movie.info.uncensored or movie.data_src == "fc2" or should_use_ai_crop_match(movie.info.label.upper()):
        crop_engine = cfg.summarizer.cover.crop.engine
    cropper = get_cropper(crop_engine)
    if portrait_path and crop_engine is None:
        with Image.open(portrait_path) as portrait_image:
            fanart_cropped = portrait_image.copy()
    else:
        with Image.open(movie.fanart_file) as fanart_image:
            fanart_cropped = cropper.crop(fanart_image)

    if cfg.summarizer.cover.add_label:
        if movie.hard_sub:
            fanart_cropped = add_label_to_poster(fanart_cropped, SUBTITLE_MARK_FILE, LabelPosition.BOTTOM_RIGHT)
        if movie.uncensored:
            fanart_cropped = add_label_to_poster(fanart_cropped, UNCENSORED_MARK_FILE, LabelPosition.BOTTOM_LEFT)
    if os.path.splitext(movie.poster_file)[1].lower() in (".jpg", ".jpeg"):
        fanart_cropped.save(movie.poster_file, quality=95, subsampling=0)
    else:
        fanart_cropped.save(movie.poster_file)

    folder_cover = folder_cover_path or os.path.join(movie.save_dir, "folder.jpg")
    # 自定义命名已使用 folder.jpg 时保留原图，避免缩略图覆盖海报或横版封面。
    image_paths = {os.path.abspath(movie.poster_file), os.path.abspath(movie.fanart_file)}
    if generate_folder_cover and os.path.abspath(folder_cover) not in image_paths:
        with fanart_cropped.convert("RGB") as folder_image:
            folder_image.save(folder_cover, format="JPEG", quality=95, subsampling=0)
