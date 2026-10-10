"""带对象身份约束的分片下载与断点续传。"""

import logging
import re
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import requests
from tqdm import tqdm

CHUNK_SIZE = 1024 * 1024
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")


class DownloadError(Exception):
    """下载响应与预期对象不符。"""


class RangeUnsupported(DownloadError):
    """服务器或代理未可靠执行范围请求。"""


class ObjectChanged(DownloadError):
    """下载过程中远端对象已变化。"""


@dataclass(frozen=True)
class DownloadSpec:
    url: str
    size: int
    prefix: Path
    etag: str | None = None


def parts(spec: DownloadSpec) -> list[tuple[int, int, Path]]:
    return [(i * spec.size // 4, (i + 1) * spec.size // 4 - 1, Path(f"{spec.prefix}.part{i}")) for i in range(4)]


def clear_parts(spec: DownloadSpec) -> None:
    for _, _, part in parts(spec):
        part.unlink(missing_ok=True)
    Path(f"{spec.prefix}.single.part").unlink(missing_ok=True)


def _part_length(path: Path, maximum: int) -> int:
    if not path.exists():
        return 0
    length = path.stat().st_size
    if length > maximum:
        path.unlink()
        return 0
    return length


def _range_matches(response, start: int, end: int, size: int) -> bool:
    match = _CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
    return response.status_code == 206 and match is not None and tuple(map(int, match.groups())) == (start, end, size)


def _headers(spec: DownloadSpec, requested_range: str | None = None) -> dict[str, str]:
    headers = {"Accept-Encoding": "identity"}
    if requested_range is not None:
        headers["Range"] = requested_range
    if spec.etag is not None:
        headers["If-Match"] = spec.etag
    return headers


def _check_identity(response, spec: DownloadSpec) -> None:
    if response.status_code == 412:
        raise ObjectChanged("下载期间对象 ETag 已变化")
    response.raise_for_status()
    if spec.etag is not None and response.headers.get("ETag") != spec.etag:
        raise ObjectChanged("下载响应 ETag 与对象列表不一致")


class DownloadProgress:
    def __init__(self, size: int, initial: int, label: str, logger: logging.Logger):
        self.size = size
        self.current = initial
        self.label = label
        self.logger = logger
        self.lock = threading.Lock()
        # 控制台日志经 tqdm.write 输出到 stdout；进度条也使用同一输出流。
        self.terminal = sys.stdout.isatty()
        self.last_percent = initial * 100 // size
        self.last_report_at = time.monotonic()
        self.bar = tqdm(
            total=size,
            initial=initial,
            desc=label,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            file=sys.stdout,
            disable=not self.terminal,
        )
        if not self.terminal:
            self._log()

    def _log(self):
        self.logger.info("%s: %d%% (%d/%d 字节)", self.label, min(self.current * 100 // self.size, 100), self.current, self.size)

    def update(self, count: int):
        with self.lock:
            self.current += count
            self.bar.update(count)
            if not self.terminal:
                now = time.monotonic()
                percent = min(self.current * 100 // self.size, 100)
                if percent >= self.last_percent + 5 or now - self.last_report_at >= 10 or self.current == self.size:
                    self._log()
                    self.last_percent = percent
                    self.last_report_at = now

    def close(self):
        self.bar.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _save_response(response, path: Path, expected: int, progress: DownloadProgress, mode: str) -> None:
    remaining = expected
    with path.open(mode) as file:
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            if chunk:
                if len(chunk) > remaining:
                    raise DownloadError("下载响应超过预期大小")
                file.write(chunk)
                remaining -= len(chunk)
                progress.update(len(chunk))
    if remaining:
        raise requests.ConnectionError("下载响应提前结束")


def _download_part(spec: DownloadSpec, part, options, progress: DownloadProgress) -> None:
    start, end, path = part
    offset = _part_length(path, end - start + 1)
    if start + offset > end:
        return
    # requests.Session 不保证跨线程安全，每个分片独立创建连接池。
    with requests.Session() as client:
        with client.get(
            spec.url,
            headers=_headers(spec, f"bytes={start + offset}-{end}"),
            stream=True,
            timeout=(30, 120),
            **options,
        ) as response:
            _check_identity(response, spec)
            if not _range_matches(response, start + offset, end, spec.size):
                raise RangeUnsupported(f"分片范围响应无效: {response.status_code} {response.headers.get('Content-Range')}")
            _save_response(response, path, end - start + 1 - offset, progress, "ab")


def _download_single(spec: DownloadSpec, client, options, progress: DownloadProgress) -> None:
    path = Path(f"{spec.prefix}.single.part")
    offset = _part_length(path, spec.size)
    if offset == spec.size:
        return
    requested_range = f"bytes={offset}-{spec.size - 1}" if offset else None
    with client.get(spec.url, headers=_headers(spec, requested_range), stream=True, timeout=(30, 120), **options) as response:
        _check_identity(response, spec)
        if offset and not _range_matches(response, offset, spec.size - 1, spec.size):
            if response.status_code == 200:
                progress.update(-offset)
                _save_response(response, path, spec.size, progress, "wb")
                return
            response.close()
            with client.get(spec.url, headers=_headers(spec), stream=True, timeout=(30, 120), **options) as fresh:
                _check_identity(fresh, spec)
                progress.update(-offset)
                _save_response(fresh, path, spec.size, progress, "wb")
            return
        _save_response(response, path, spec.size - offset, progress, "ab" if offset else "wb")


def download_file(
    spec: DownloadSpec,
    *,
    client,
    options: dict,
    label: str,
    logger: logging.Logger,
) -> Path:
    """返回已组装的临时文件；调用方验证并发布后清理分片。"""
    if spec.size <= 0:
        raise ValueError("下载对象大小必须为正数")
    temporary = None
    with client.get(spec.url, headers=_headers(spec, "bytes=0-0"), stream=True, timeout=(30, 120), **options) as probe:
        _check_identity(probe, spec)
        ranged = _range_matches(probe, 0, 0, spec.size)
        if probe.status_code == 206 and not ranged:
            logger.warning("范围探测响应无效，回退单连接")
    if ranged:
        segments = parts(spec)
        initial = sum(_part_length(part, end - start + 1) for start, end, part in segments)
        with DownloadProgress(spec.size, initial, label, logger) as progress:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(_download_part, spec, part, options, progress) for part in segments]
                errors = []
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as error:
                        errors.append(error)
        if errors:
            changed = next((error for error in errors if isinstance(error, ObjectChanged)), None)
            if changed is not None:
                raise changed
            range_error = next((error for error in errors if isinstance(error, RangeUnsupported)), None)
            if range_error is None:
                raise errors[0]
            logger.warning("分片下载不可用，回退单连接: %s", range_error)
            ranged = False
    if not ranged:
        logger.info("下载源不支持可靠分片，改用单连接")
        single = Path(f"{spec.prefix}.single.part")
        with DownloadProgress(spec.size, _part_length(single, spec.size), label, logger) as progress:
            _download_single(spec, client, options, progress)

    try:
        with tempfile.NamedTemporaryFile(
            prefix=f"{spec.prefix.name}.", suffix=".assembling", dir=spec.prefix.parent, delete=False
        ) as file:
            temporary = Path(file.name)
            sources = [part for _, _, part in parts(spec)] if ranged else [Path(f"{spec.prefix}.single.part")]
            for source_path in sources:
                with source_path.open("rb") as source:
                    shutil.copyfileobj(source, file, CHUNK_SIZE)
        if temporary.stat().st_size != spec.size:
            raise DownloadError("组装文件大小与对象列表不一致")
        return temporary
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
