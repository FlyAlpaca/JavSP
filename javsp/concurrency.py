"""取消时仍等待实际工作结束，避免清理目录后后台任务继续写入。"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait


def drain_threads(workers):
    """done 由工作函数 finally 设置，不依赖被中断 join 的存活状态。"""
    interruption = None
    for thread, done in workers:
        while not done.is_set():
            try:
                done.wait()
            except (KeyboardInterrupt, SystemExit) as error:
                interruption = error
        while True:
            try:
                thread.join()
                break
            except (KeyboardInterrupt, SystemExit) as error:
                interruption = error
    if interruption:
        raise interruption


def finish_executor(executor, futures):
    """shutdown 被中断时，先等待 Future 完成，再回收线程并传播取消。"""
    interruption = None
    done = threading.Event()

    def completed(_):
        if all(future.done() for future in futures):
            done.set()

    for future in futures:
        future.add_done_callback(completed)
    completed(None)
    while True:
        try:
            executor.shutdown(wait=True, cancel_futures=True)
            break
        except (KeyboardInterrupt, SystemExit) as error:
            interruption = error
            # Future.done() 也包含被取消的排队任务，避免 wait 的取消状态差异。
            while not done.is_set():
                try:
                    done.wait()
                except (KeyboardInterrupt, SystemExit) as error:
                    interruption = error
    if interruption:
        raise interruption


class BatchCancelled(KeyboardInterrupt):
    """批次停止后，尚未提交的任务在安全边界退出。"""


def await_futures(futures):
    """仅回收当前调用的任务，不关闭共用线程池；反复取消也不能跳过等待。"""
    interruption = None
    for future in futures:
        while not future.done():
            try:
                wait([future], timeout=0.05)
            except (KeyboardInterrupt, SystemExit) as error:
                interruption = error
    if interruption:
        raise interruption


class BatchResources:
    """批次专用线程池、站点冷却、剧照节流与停止信号。"""

    def __init__(self, cfg):
        self.stopped = threading.Event()
        self.crawlers = ThreadPoolExecutor(max_workers=cfg.concurrency.crawlers, thread_name_prefix="crawl")
        self.images = ThreadPoolExecutor(max_workers=cfg.concurrency.images, thread_name_prefix="image")
        self.site_interval = cfg.crawler.sleep_after_scraping.total_seconds()
        self._sites_lock = threading.Lock()
        self._sites = {}
        self._fanart_lock = threading.Lock()
        self._next_fanart = 0.0
        self.translation_slots = threading.BoundedSemaphore(cfg.translator.runtime.parallel)
        self._futures = set()
        self._futures_lock = threading.Lock()

    def check(self):
        if self.stopped.is_set():
            raise BatchCancelled("批次已停止")

    def pause(self, seconds):
        if self.stopped.wait(max(0, seconds)):
            self.check()

    def submit(self, pool, function, *args):
        self.check()
        future = pool.submit(function, *args)
        with self._futures_lock:
            self._futures.add(future)
        def completed(done):
            with self._futures_lock:
                self._futures.discard(done)
        future.add_done_callback(completed)
        return future

    def site_task(self, site, function, started):
        with self._sites_lock:
            lock, next_start = self._sites.setdefault(site, (threading.Lock(), [0.0]))
        while not lock.acquire(timeout=0.05):
            self.check()
        try:
            self.pause(next_start[0] - time.monotonic())
            self.check()
            started[0] = time.monotonic()
            try:
                return function()
            finally:
                started[1] = time.monotonic()
        finally:
            next_start[0] = time.monotonic() + self.site_interval
            lock.release()

    def fanart_start(self, interval):
        with self._fanart_lock:
            self.pause(self._next_fanart - time.monotonic())
            self._next_fanart = time.monotonic() + max(0, interval)

    def close(self):
        """调用方先回收影片线程，再关闭子任务池。"""
        interruption = None
        for pool in (self.crawlers, self.images):
            try:
                with self._futures_lock:
                    futures = list(self._futures)
                finish_executor(pool, futures)
            except (KeyboardInterrupt, SystemExit) as error:
                self.stopped.set()
                interruption = error
        if interruption:
            raise interruption
