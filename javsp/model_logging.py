"""将 llama-server 输出接入应用日志，并保留有限的启动诊断。"""

import json
import logging
import re
import threading
from collections import deque

logger = logging.getLogger("javsp.translate.model")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_PREFIX = re.compile(r"^(?:\d+\.\d{2}\.\d{3}\.\d{3}\s+)?([IWED])\s+(.*)$")
_LEVELS = {"warn": logging.WARNING, "warning": logging.WARNING, "error": logging.ERROR, "W": logging.WARNING, "E": logging.ERROR}


def _record(line, previous_level=logging.DEBUG):
    """原生 INFO/DEBUG 均为排查明细；保留 WARNING/ERROR 的严重性。"""
    message = _ANSI.sub("", line).strip()
    if not message:
        return None
    try:
        record = json.loads(message)
    except ValueError:
        record = None
    if isinstance(record, dict) and record.get("type") == "log" and isinstance(record.get("msg"), str):
        level = record.get("level")
        if not isinstance(level, str):
            level = "info"
        return previous_level if level == "cont" else _LEVELS.get(level, logging.DEBUG), record["msg"].strip()
    if isinstance(record, dict):
        # 非日志事件可能携带请求内容，不输出整个事件。
        return None
    match = _PREFIX.fullmatch(message)
    if match:
        return _LEVELS.get(match[1], logging.DEBUG), match[2]
    # 参数解析失败和部分底层库输出仍可能使用纯文本。
    if re.search(r"\b(error|fatal)\s*:", message, re.IGNORECASE):
        return logging.ERROR, message
    if re.search(r"\bwarn(?:ing)?\s*:", message, re.IGNORECASE):
        return logging.WARNING, message
    return logging.DEBUG, message


class ModelLogReader:
    """持续排空合并的输出管道；调用方先结束进程，再等待读取线程。"""

    def __init__(self):
        self._tail = deque(maxlen=10)
        self._thread = None
        self._stream = None

    def start(self, stream):
        self._tail.clear()
        self._stream = stream
        thread = threading.Thread(target=self._read, name="javsp-model-log")
        thread.start()
        self._thread = thread

    def _read(self):
        previous_level = logging.DEBUG
        try:
            for line in self._stream:
                record = _record(line, previous_level)
                if record is None:
                    continue
                level, message = record
                previous_level = level
                if not message:
                    continue
                self._tail.append(message[:1000])
                # 每行使用应用时间戳和格式，多行原生日志也保持统一。
                for part in message.splitlines():
                    logger.log(level, "%s", part)
        except (OSError, ValueError) as error:
            logger.warning("读取模型日志失败: %s", error)

    def close(self):
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    @property
    def diagnostic(self):
        """仅在读取线程结束后读取，避免遗漏启动退出前的最后几行。"""
        return " | ".join(" ".join(message.splitlines()) for message in self._tail)
