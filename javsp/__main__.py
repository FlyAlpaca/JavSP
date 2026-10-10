"""JavSP 容器入口：配置初始化和批处理编排。"""

import logging
import os
import signal
import sys

from pydantic import ValidationError

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

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

from javsp.config import Cfg
from javsp.datatype import setup_filemove_log
from javsp.startup import StartupError, startup_context
from javsp.translate import TranslateError
from javsp.workflow.models import Stage
from javsp.workflow.pipeline import JobRunner, Pipeline


def _process_single_movie(movie, total_step, stats, cfg, translator=None):
    """兼容单片入口，实际编排由 JobRunner 完成。"""
    job = JobRunner(cfg, translator=translator).run(movie)
    if job.error:
        raise job.error.cause
    return job.output


def RunNormalMode(all_movies, cfg, translator=None):
    """CLI 进度适配，保留 (successful_movies, stats) 返回格式。"""
    total_step = 6
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
        batch = JobRunner(cfg, on_event, translator=translator).run_batch(all_movies, on_result)
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


def _run_with_translator(cfg, root, translator):
    """全部启动资源就绪后扫描和处理媒体文件。"""
    if cfg.other.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    os.chdir(root)
    setup_filemove_log(cfg, log_dir=root)

    print("扫描影片文件...")
    scan_result = Pipeline(cfg, translator=translator).scan(root)
    recognized = scan_result.movies
    error_exit(len(recognized), "未找到影片文件")
    logger.info(f"扫描影片文件：共找到 {len(recognized)} 部影片")
    _, stats = RunNormalMode(recognized, cfg, translator)
    print_summary(stats)
    return 1 if stats["failed"] or stats.get("aborted") else 0


def entry():
    try:
        cfg = Cfg()
    except ValidationError as e:
        for err in e.errors():
            loc = " → ".join(str(part) for part in err["loc"])
            msg = err["msg"]
            print(f"配置错误 [{loc}]: {msg}")
        sys.exit(1)

    def stop_on_term(signum, _frame):
        raise SystemExit(128 + signum)

    previous_handler = signal.signal(signal.SIGTERM, stop_on_term)
    try:
        try:
            with startup_context(cfg) as (root, translator):
                result = _run_with_translator(cfg, root, translator)
        except StartupError as error:
            logger.error("启动检查失败: %s", error)
            sys.exit(1)
        except TranslateError as error:
            logger.error("本地翻译启动失败: %s", error)
            sys.exit(1)
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    sys.exit(result)


if __name__ == "__main__":
    try:
        entry()
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)
