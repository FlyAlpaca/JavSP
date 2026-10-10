"""JavSP 入口：配置初始化、主流程编排、用户交互"""

import logging
import os
import sys
import time

from pydantic import ValidationError

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import colorama
from colorama import Fore, Style
from tqdm import tqdm

from javsp.print import TqdmOut, install

# 将StreamHandler的stream修改为TqdmOut，以与Tqdm协同工作
install()
# 初始化 logging，输出到 stdout（Docker 实时日志窗口默认只显示 stdout）
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
    stream=sys.stdout,
)

# 关闭第三方库的 DEBUG 日志
logging.getLogger("PIL").setLevel(logging.WARNING)

# 将毫秒分隔符从逗号改为点号，并将 StreamHandler 的 stream 改为 TqdmOut 以与 tqdm 协同
root_logger = logging.getLogger()
for handler in root_logger.handlers:
    handler.formatter.default_msec_format = "%s.%03d"
    if type(handler) is logging.StreamHandler:
        handler.stream = TqdmOut

logger = logging.getLogger("main")

from javsp.__version__ import __version__
from javsp.config import Cfg
from javsp.datatype import Movie, setup_filemove_log
from javsp.dispatcher import import_crawlers
from javsp.func import check_update, get_scan_dir
from javsp.lib import prompt
from javsp.models import Stage
from javsp.pipeline import JobRunner, Pipeline
from javsp.r18dev import prepare_database
from javsp.summarizer import load_actress_aliases


def reviewMovieID(all_movies, root):
    """人工检查每一部影片的番号"""
    count = len(all_movies)
    logger.info("进入手动模式检查番号: ")
    for i, movie in enumerate(all_movies, start=1):
        id = movie.dvdid or movie.cid or "未知番号"
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
            new_id = new_movie.dvdid or new_movie.cid or "未知番号"
            logger.info(f"已更正影片番号: {','.join(relpaths)}: {id} -> {new_id}")
        print()


def _process_single_movie(movie, total_step, stats, cfg):
    """兼容单片入口，实际编排由 JobRunner 完成。"""
    job = JobRunner(cfg).run(movie)
    if job.error:
        raise job.error.cause
    return job.output


def RunNormalMode(all_movies, cfg):
    """CLI 进度适配，保留 (successful_movies, stats) 返回格式。"""
    total_step = 5 + bool(cfg.translator.engine)
    outer_bar = tqdm(total=len(all_movies), desc="整理影片", ascii=True, leave=False)
    inner_bar = None

    def on_event(event):
        nonlocal inner_bar
        if event.stage == Stage.CRAWL and not event.detail:
            if inner_bar:
                inner_bar.close()
            inner_bar = tqdm(total=total_step, desc="步骤", ascii=True, leave=False)
        if inner_bar:
            inner_bar.set_description(event.detail or str(event.stage))
            if not event.detail and event.stage not in (Stage.CRAWL, Stage.ROLLBACK):
                inner_bar.update()

    def on_result(job):
        nonlocal inner_bar
        if inner_bar:
            inner_bar.close()
            inner_bar = None
        outer_bar.update()

    try:
        batch = JobRunner(cfg, on_event).run_batch(all_movies, on_result)
        return batch.legacy(cfg.summarizer.filemove_log)
    finally:
        if inner_bar:
            inner_bar.close()
        outer_bar.close()


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
    if stats["success_list"]:
        print("-" * width)
        print("  成功详情:")
        for movie_id, source_summary in stats["success_list"]:
            print(f"    {movie_id}")
            if source_summary:
                print(f"      {source_summary}")
    if stats["failed_list"]:
        print("-" * width)
        print("  失败详情:")
        for movie_id, reason in stats["failed_list"]:
            # 截断过长的原因
            short_reason = reason if len(reason) <= 60 else reason[:57] + "..."
            print(f"    {movie_id}: {short_reason}")
    if stats["filemove_list"]:
        print("-" * width)
        print("  文件移动详情:")
        for movie_id, src, dst, source_info in stats["filemove_list"]:
            src_rel = os.path.relpath(src)
            dst_rel = os.path.relpath(dst)
            print(f"    [{movie_id}]{source_info}")
            print(f"      {src_rel}")
            print(f"      -> {dst_rel}")
    if stats.get("pending_list"):
        print("-" * width)
        print("  未处理影片: " + ", ".join(stats["pending_list"]))
    if stats.get("batch_error"):
        print("  批次终止原因: " + stats["batch_error"])
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
        cfg = Cfg()
    except ValidationError as e:
        for err in e.errors():
            loc = " → ".join(str(part) for part in err["loc"])
            msg = err["msg"]
            print(f"配置错误 [{loc}]: {msg}")
        input("按回车键退出...")
        exit(1)

    load_actress_aliases()

    # 启用 debug 模式时将根日志级别设为 DEBUG
    if cfg.other.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    colorama.init(autoreset=True)

    # 检查更新
    version_info = f"JavSP {__version__}"
    logger.debug(version_info.center(60, "="))
    check_update(cfg.other.check_update, cfg.other.auto_update)
    if getattr(getattr(cfg, "r18dev", None), "enabled", False):
        cfg._r18dev_db_path = prepare_database(cfg._r18dev_db_path, cfg.crawler.online_enabled)
    root = get_scan_dir(cfg.scanner.input_directory)
    error_exit(root, "未选择要扫描的文件夹")
    # 导入抓取器，必须在chdir之前
    import_crawlers(cfg)
    os.chdir(root)

    # 配置文件移动日志（显式指定日志目录为扫描根目录，避免依赖全局CWD）
    setup_filemove_log(cfg, log_dir=root)

    print("扫描影片文件...")
    scan_result = Pipeline(cfg).scan(root)
    recognized = scan_result.movies
    movie_count = len(recognized)
    error_exit(movie_count, "未找到影片文件")
    logger.info(f"扫描影片文件：共找到 {movie_count} 部影片")
    if cfg.scanner.manual:
        reviewMovieID(recognized, root)
    _, stats = RunNormalMode(recognized, cfg)

    print_summary(stats)
    if cfg.other.auto_exit:
        if cfg.other.interactive:
            wait_exit(5)
        else:
            time.sleep(5)
    else:
        input("整理完成，按回车键退出...")
    sys.exit(1 if stats["failed"] or stats.get("aborted") else 0)


if __name__ == "__main__":
    try:
        entry()
    except Exception as e:
        import traceback

        traceback.print_exc()
        input(f"程序发生错误: {e}\n按回车键退出...")
        sys.exit(1)
