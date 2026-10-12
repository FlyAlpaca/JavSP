"""图片下载的通用网络接口，与元数据查询独立。"""

import contextlib
import logging
import os
import shutil
import time

import requests
from tqdm import tqdm

logger = logging.getLogger(__name__)
headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    )
}


def read_proxy(network):
    if network.proxy_server is None:
        return {}
    return {"http": str(network.proxy_server), "https": str(network.proxy_server)}


class Request:
    """每次下载独立创建并关闭请求会话。"""

    def __init__(self, network):
        self.headers = headers.copy()
        self.proxies = read_proxy(network)
        self.timeout = network.timeout.total_seconds()
        self.session = requests.Session()

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def get(self, url, timeout=None, delay_raise=False, stream=False):
        response = self.session.get(
            url, headers=self.headers, proxies=self.proxies,
            timeout=timeout if timeout is not None else self.timeout, stream=stream,
        )
        if not delay_raise:
            response.raise_for_status()
        return response


class DownloadProgressBar(tqdm):
    def update_to(self, b=1, bsize=1, tsize=None):
        if tsize is not None:
            self.total = tsize
        self.update(b * bsize - self.n)


def urlretrieve(url, filename=None, reporthook=None, extra_headers=None, *, network):
    """下载文件，走统一的 Request 出口"""
    with Request(network) as req:
        return _urlretrieve(req, url, filename, reporthook, extra_headers)


def _urlretrieve(req, url, filename, reporthook, extra_headers):
    if extra_headers:
        for k, v in extra_headers.items():
            req.headers[k] = v
    with contextlib.closing(req.get(url, timeout=req.timeout * 3, stream=True, delay_raise=True)) as r:
        r.raise_for_status()
        header = r.headers
        with open(filename, "wb") as fp:
            size = -1
            downloaded = 0
            if "content-length" in header:
                size = int(header["Content-Length"])  # 文件总大小（理论值）
            if reporthook:  # 写入前运行一次回调函数
                reporthook(downloaded, 1, size)
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    fp.write(chunk)
                    downloaded += len(chunk)
                    if reporthook:
                        reporthook(downloaded, 1, size)  # 按实际收到的字节数更新进度


def download(url, output_path, desc=None, *, network):
    """下载指定url的资源"""
    # 支持本地图片，用于组件调用与离线测试
    if not url.startswith(("http://", "https://")):
        start_time = time.time()
        shutil.copyfile(url, output_path)
        filesize = os.path.getsize(url)
        elapsed = time.time() - start_time
        info = {"total": filesize, "elapsed": elapsed, "rate": filesize / elapsed}
        return info
    if not desc:
        desc = url.split("/")[-1]
    referrer = headers.copy()
    slash_pos = url.find("/", 8)
    referrer["referer"] = url[: slash_pos + 1] if slash_pos != -1 else url + "/"
    with DownloadProgressBar(unit="B", unit_scale=True, miniters=1, desc=desc, leave=False) as t:
        urlretrieve(url, filename=output_path, reporthook=t.update_to, extra_headers=referrer, network=network)
        info = {k: t.format_dict[k] for k in ("total", "elapsed", "rate")}
        return info
