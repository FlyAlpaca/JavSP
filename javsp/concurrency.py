"""取消时仍等待实际工作结束，避免清理目录后后台任务继续写入。"""

import threading


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
