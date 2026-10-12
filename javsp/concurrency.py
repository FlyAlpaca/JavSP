"""取消时仍等待实际工作结束，避免清理目录后后台任务继续写入。"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait


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
    """批次图片线程池、剧照节流与停止信号。"""

    def __init__(self, cfg):
        self.stopped = threading.Event()
        self.images = ThreadPoolExecutor(max_workers=cfg.concurrency.images, thread_name_prefix="image")
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

    def fanart_start(self, interval):
        with self._fanart_lock:
            self.pause(self._next_fanart - time.monotonic())
            self._next_fanart = time.monotonic() + max(0, interval)

    def close(self):
        """调用方先回收影片线程，再关闭子任务池。"""
        try:
            with self._futures_lock:
                futures = list(self._futures)
            finish_executor(self.images, futures)
        except (KeyboardInterrupt, SystemExit):
            self.stopped.set()
            raise
