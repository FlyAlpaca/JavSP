"""JavSP 容器入口：配置初始化和批处理编排。"""

import logging
import signal
import sys

from pydantic import ValidationError

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


from javsp.logging_config import configure_logging

logger = logging.getLogger("main")

from javsp.cli import ProgressDisplay, print_summary, summarize_batch
from javsp.config import ConfigLoadError, load_config
from javsp.datatype import setup_filemove_log
from javsp.startup import StartupError, startup_context
from javsp.translate import TranslateError
from javsp.workflow.pipeline import Pipeline


def run_batch(runtime):
    """启动资源就绪后通过统一流水线处理影片。"""
    cfg = runtime.cfg
    root = runtime.root
    setup_filemove_log(cfg, log_dir=root)
    print("扫描影片文件...")
    with ProgressDisplay() as progress:
        batch = Pipeline(cfg, progress.on_event, runtime=runtime).run(
            root, review=progress.review, on_result=progress.on_result
        )
    _, stats = summarize_batch(batch, cfg.summarizer.filemove_log)
    print_summary(stats)
    return batch.exit_code


def entry():
    configure_logging()
    try:
        cfg = load_config()
    except ConfigLoadError as error:
        logger.error("配置加载失败: %s", error)
        sys.exit(1)
    except ValidationError as e:
        for err in e.errors():
            loc = " → ".join(str(part) for part in err["loc"])
            msg = err["msg"]
            print(f"配置错误 [{loc}]: {msg}")
        sys.exit(1)

    configure_logging(cfg.other.debug)

    def stop_on_term(signum, _frame):
        raise SystemExit(128 + signum)

    previous_handler = signal.signal(signal.SIGTERM, stop_on_term)
    try:
        try:
            with startup_context(cfg) as runtime:
                result = run_batch(runtime)
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
