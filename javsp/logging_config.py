"""命令行和独立抓取器共用的控制台日志配置。"""

import logging

from javsp.print import TqdmOut, install

_FORMAT = "%(asctime)s %(levelname)s:%(name)s: %(message)s"


def configure_logging(debug=False):
    """配置与进度条兼容的 stdout 日志，可安全重复调用。"""
    install()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    if not any(getattr(handler, "_javsp_console", False) for handler in root.handlers):
        handler = logging.StreamHandler(TqdmOut)
        handler._javsp_console = True
        formatter = logging.Formatter(_FORMAT)
        formatter.default_msec_format = "%s.%03d"
        handler.setFormatter(formatter)
        root.addHandler(handler)
    logging.getLogger("PIL").setLevel(logging.WARNING)
