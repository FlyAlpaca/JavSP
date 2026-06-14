"""JavSP 入口：配置初始化、主流程编排、用户交互"""

import logging
import os
import sys
import time

from pydantic import ValidationError
from pydantic_extra_types.pendulum_dt import Duration

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import colorama
import pretty_errors
from colorama import Fore, Style
from tqdm import tqdm

pretty_errors.configure(display_link=True)

from javsp.print import TqdmOut

# 将StreamHandler的stream修改为TqdmOut，以与Tqdm协同工作
root_logger = logging.getLogger()
for handler in root_logger.handlers:
    if type(handler) is logging.StreamHandler:
        handler.stream = TqdmOut

logger = logging.getLogger("main")

from javsp.__version__ import __version__
from javsp.config import Cfg
from javsp.datatype import Movie
from javsp.dispatcher import import_crawlers, parallel_crawler
from javsp.file import get_fmt_size, scan_movies
from javsp.func import check_update, get_scan_dir
from javsp.image import get_pic_size, valid_pic
from javsp.lib import prompt
from javsp.nfo import write_nfo
from javsp.processor import download_cover, generate_names, process_poster
from javsp.summarizer import info_summary, load_actress_aliases
from javsp.web.base import download
from javsp.web.translate import translate_movie_info


def reviewMovieID(all_movies, root):
    """人工检查每一部影片的番号"""
    count = len(all_movies)
    logger.info("进入手动模式检查番号: ")
    for i, movie in enumerate(all_movies, start=1):
        id = repr(movie)[7:-2]
        print(f"[{i}/{count}]\t{Fore.LIGHTMAGENTA_EX}{id}{Style.RESET_ALL}, 对应文件:")
        relpaths = [os.path.relpath(i, root) for i in movie.files]
        print("\n".join(["  " + i for i in relpaths]))
        s = prompt(
            "回车确认当前番号，或直接输入更正后的番号（如'ABC-123'或'cid:sqte00300'）",
            "更正后的番号",
        )
        if not s:
            logger.info(f"已确认影片番号: {','.join(relpaths)}: {id}")
        else:
            s = s.strip()
            s_lc = s.lower()
            if s_lc.startswith(("cid:", "cid=")):
                new_movie = Movie(cid=s_lc[4:])
                new_movie.data_src = "cid"
                new_movie.files = movie.files
            elif s_lc.startswith("fc2"):
                new_movie = Movie(s)
                new_movie.data_src = "fc2"
                new_movie.files = movie.files
            else:
                new_movie = Movie(s)
                new_movie.data_src = "normal"
                new_movie.files = movie.files
            all_movies[i - 1] = new_movie
            new_id = repr(new_movie)[7:-2]
            logger.info(f"已更正影片番号: {','.join(relpaths)}: {id} -> {new_id}")
        print()


def RunNormalMode(all_movies):
    """普通整理模式"""

    # 运行统计
    stats = {"total": len(all_movies), "success": 0, "failed": 0, "failed_list": []}

    def check_step(result, msg="步骤错误"):
        """检查一个整理步骤的结果，并负责更新tqdm的进度"""
        if result:
            inner_bar.update()
        else:
            movie_id = movie.dvdid or movie.cid or "未知番号"
            raise Exception(f"[{movie_id}] {msg}")

    def step_with_id(step_name, fn):
        """执行步骤，为异常补充番号上下文"""
        try:
            fn()
            inner_bar.update()
        except Exception as e:
            movie_id = movie.dvdid or movie.cid or "未知番号"
            # 如果异常信息已包含番号则不再重复添加
            if movie_id in str(e):
                raise
            raise Exception(f"[{movie_id}] {step_name}: {e}") from e

    outer_bar = tqdm(all_movies, desc="整理影片", ascii=True, leave=False)
    total_step = 6
    if Cfg().translator.engine:
        total_step += 1
    if Cfg().summarizer.extra_fanarts.enabled:
        total_step += 1

    return_movies = []
    for movie in outer_bar:
        try:
            # 初始化本次循环要整理影片任务
            filenames = [os.path.split(i)[1] for i in movie.files]
            logger.info("正在整理: " + ", ".join(filenames))
            inner_bar = tqdm(total=total_step, desc="步骤", ascii=True, leave=False)
            # 依次执行各个步骤
            inner_bar.set_description("启动并发任务")
            all_info = parallel_crawler(movie, inner_bar)
            inner_bar.update()

            inner_bar.set_description("汇总数据")
            missing_keys = info_summary(movie, all_info)
            if missing_keys:
                movie_id = movie.dvdid or movie.cid or "未知番号"
                raise Exception(f"[{movie_id}] 汇总数据失败：必需字段缺失 ({missing_keys})")
            inner_bar.update()

            if Cfg().translator.engine:
                inner_bar.set_description("翻译影片信息")
                step_with_id("翻译影片信息", lambda: translate_movie_info(movie.info))

            inner_bar.set_description("生成文件名")
            step_with_id("生成文件名", lambda: generate_names(movie))
            check_step(movie.save_dir, "无法按命名规则生成目标文件夹")
            try:
                if not os.path.exists(movie.save_dir):
                    os.makedirs(movie.save_dir)
            except OSError as e:
                movie_id = movie.dvdid or movie.cid or "未知番号"
                raise Exception(f"[{movie_id}] 创建目标文件夹失败: {movie.save_dir}: {e}") from e

            inner_bar.set_description("下载封面图片")
            movie_id = movie.dvdid or movie.cid or "未知番号"
            if Cfg().summarizer.cover.highres:
                cover_dl = download_cover(
                    movie.info.covers,
                    movie.fanart_file,
                    movie.info.big_covers,
                    movie_id=movie_id,
                )
            else:
                cover_dl = download_cover(
                    movie.info.covers,
                    movie.fanart_file,
                    movie_id=movie_id,
                )
            if not cover_dl or cover_dl[0] is None:
                reason = cover_dl[1] if cover_dl else "未知原因"
                raise Exception(f"[{movie_id}] 下载封面图片失败: {reason}")
            inner_bar.update()
            cover, pic_path = cover_dl
            # 确保实际下载的封面的url与即将写入到movie.info中的一致
            if cover != movie.info.cover:
                movie.info.cover = cover
            # 根据实际下载的封面的格式更新fanart/poster等图片的文件名
            if pic_path != movie.fanart_file:
                movie.fanart_file = pic_path
                actual_ext = os.path.splitext(pic_path)[1]
                movie.poster_file = os.path.splitext(movie.poster_file)[0] + actual_ext

            inner_bar.set_description("处理封面")
            step_with_id("处理封面", lambda: process_poster(movie))

            if Cfg().summarizer.extra_fanarts.enabled:
                scrape_interval = Cfg().summarizer.extra_fanarts.scrap_interval.total_seconds()
                inner_bar.set_description("下载剧照")
                if movie.info.preview_pics:
                    extrafanartdir = os.path.join(movie.save_dir, "extrafanart")
                    os.makedirs(extrafanartdir, exist_ok=True)
                    for idx, pic_url in enumerate(movie.info.preview_pics):
                        inner_bar.set_description(f"下载剧照 {idx}")

                        fanart_destination = os.path.join(extrafanartdir, f"{idx}.png")
                        try:
                            info = download(pic_url, fanart_destination)
                            if valid_pic(fanart_destination):
                                filesize = get_fmt_size(fanart_destination)
                                width, height = get_pic_size(fanart_destination)
                                elapsed = time.strftime("%M:%S", time.gmtime(info["elapsed"]))
                                speed = get_fmt_size(info["rate"]) + "/s"
                                logger.info(f"已下载剧照{pic_url} {idx}.png: {width}x{height}, {filesize} [{elapsed}, {speed}]")
                            else:
                                logger.warning(f"下载剧照{idx}失败: {pic_url}")
                        except Exception as e:
                            logger.warning(f"下载剧照{idx}失败: {pic_url}, {e}")
                        time.sleep(scrape_interval)
                check_step(True)

            inner_bar.set_description("写入NFO")
            step_with_id("写入NFO", lambda: write_nfo(movie.info, movie.nfo_file))
            if Cfg().summarizer.move_files:
                inner_bar.set_description("移动影片文件")
                step_with_id("移动影片文件", lambda: movie.rename_files(Cfg().summarizer.path.hard_link))
                logger.info(f"整理完成，相关文件已保存到: {movie.save_dir}\n")
            else:
                logger.info(f"刮削完成，相关文件已保存到: {movie.nfo_file}\n")

            if movie != all_movies[-1] and Cfg().crawler.sleep_after_scraping > Duration(0):
                time.sleep(Cfg().crawler.sleep_after_scraping.total_seconds())
            return_movies.append(movie)
            stats["success"] += 1
        except Exception as e:
            movie_id = movie.dvdid or movie.cid or "未知番号"
            logger.error(f"整理失败: {e}")
            logger.debug(e, exc_info=True)
            stats["failed"] += 1
            stats["failed_list"].append((movie_id, str(e)))
        finally:
            inner_bar.close()
    return return_movies, stats


def error_exit(success, err_info):
    """检查业务逻辑是否成功完成，如果失败则报错退出程序"""
    if not success:
        logger.error(err_info)
        sys.exit(1)


def print_summary(stats):
    """打印运行统计摘要"""
    total = stats["total"]
    success = stats["success"]
    failed = stats["failed"]
    width = 50
    print()
    print("=" * width)
    print("  运行统计".center(width))
    print("-" * width)
    print(f"  总计: {total}  成功: {success}  失败: {failed}")
    if stats["failed_list"]:
        print("-" * width)
        print("  失败详情:")
        for movie_id, reason in stats["failed_list"]:
            # 截断过长的原因
            short_reason = reason if len(reason) <= 60 else reason[:57] + "..."
            print(f"    {movie_id}: {short_reason}")
    print("=" * width)
    print()


def wait_exit(timeout=5):
    """等待指定秒数后退出，期间按任意键可立即退出"""
    print(f"将在 {timeout} 秒后自动退出，按任意键立即退出...")
    # 非阻塞等待按键，超时后自动退出
    try:
        if sys.platform == "win32":
            import msvcrt

            # 清空键盘缓冲区中残留的按键，避免程序运行期间的按键导致立即退出
            while msvcrt.kbhit():
                msvcrt.getch()
            start = time.monotonic()
            while time.monotonic() - start < timeout:
                if msvcrt.kbhit():
                    msvcrt.getch()
                    return
                time.sleep(0.1)
        else:
            # Unix: 使用 select 监听 stdin
            import select

            start = time.monotonic()
            while time.monotonic() - start < timeout:
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    break
                r, _, _ = select.select([sys.stdin], [], [], min(remaining, 0.5))
                if r:
                    sys.stdin.read(1)
                    return
    except Exception:
        time.sleep(timeout)


def entry():
    try:
        Cfg()
    except ValidationError as e:
        for err in e.errors():
            loc = " → ".join(str(l) for l in err["loc"])
            msg = err["msg"]
            print(f"配置错误 [{loc}]: {msg}")
        input("按回车键退出...")
        exit(1)

    load_actress_aliases()

    colorama.init(autoreset=True)

    # 检查更新
    version_info = f"JavSP {__version__}"
    logger.debug(version_info.center(60, "="))
    check_update(Cfg().other.check_update, Cfg().other.auto_update)
    root = get_scan_dir(Cfg().scanner.input_directory)
    error_exit(root, "未选择要扫描的文件夹")
    # 导入抓取器，必须在chdir之前
    import_crawlers()
    os.chdir(root)

    print("扫描影片文件...")
    recognized = scan_movies(root)
    movie_count = len(recognized)
    recognize_fail = []
    error_exit(movie_count, "未找到影片文件")
    logger.info(f"扫描影片文件：共找到 {movie_count} 部影片")
    if Cfg().scanner.manual:
        reviewMovieID(recognized, root)
    _, stats = RunNormalMode(recognized + recognize_fail)

    print_summary(stats)
    if Cfg().other.interactive:
        wait_exit(5)
    else:
        time.sleep(5)
    sys.exit(0)


if __name__ == "__main__":
    try:
        entry()
    except Exception as e:
        import traceback

        traceback.print_exc()
        input(f"程序发生错误: {e}\n按回车键退出...")
        sys.exit(1)
