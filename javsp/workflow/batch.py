"""有界准备、按输入顺序提交；所有用户回调在调度线程执行。"""

import logging
from concurrent.futures import ThreadPoolExecutor, wait
from queue import Empty, SimpleQueue

from javsp.concurrency import BatchCancelled, BatchResources, finish_executor
from javsp.workflow.models import BatchResult, JobError, JobResult, Stage
from javsp.workflow.pipeline import JobRunner, PreparedJob

logger = logging.getLogger(__name__)


class _Translator:
    def __init__(self, translator, resources):
        self.translator, self.resources = translator, resources

    def translate(self, text, field):
        while not self.resources.translation_slots.acquire(timeout=0.05):
            self.resources.check()
        try:
            self.resources.check()
            result = self.translator.translate(text, field)
            self.resources.check()
            return result
        finally:
            self.resources.translation_slots.release()


def _fatal(job):
    return isinstance(job, JobResult) and (job.cancelled or (job.error and not job.error.recovered))


class ConcurrentBatch:
    def __init__(self, runner):
        self.runner = runner
        self.events = SimpleQueue()

    def _events(self):
        while True:
            try:
                event = self.events.get_nowait()
            except Empty:
                return
            if self.runner.on_event:
                try:
                    self.runner.on_event(event)
                except Exception:
                    logger.warning("进度回调失败", exc_info=True)

    @staticmethod
    def _notify(job, on_result):
        if on_result:
            try:
                on_result(job)
            except Exception:
                logger.warning("任务结果回调失败", exc_info=True)

    def run(self, movies, on_result=None):
        if self.runner.translator is None:
            raise ValueError("翻译器未初始化，请传入翻译器或已准备的运行上下文")
        movies = list(movies)
        batch = BatchResult(len(movies))
        if not movies:
            return batch
        resources = BatchResources(self.runner.cfg)
        pool = ThreadPoolExecutor(max_workers=self.runner.cfg.concurrency.movies, thread_name_prefix="movie")
        translator = _Translator(self.runner.translator, resources)
        active, completed, targets = {}, {}, set()
        submitted = 0

        def prepare(runner, movie):
            result = runner.prepare(movie)
            if _fatal(result):
                resources.stopped.set()
            return result

        def record(index, job):
            job.task_id = str(index + 1)
            completed[index] = job
            self._notify(job, on_result)
            if _fatal(job):
                batch.aborted = True
                resources.stopped.set()
                batch.error = job.error or JobError(job.movie.dvdid or job.movie.cid, Stage.DONE, job.interruption)

        try:
            for index in range(len(movies)):
                while submitted < len(movies) and len(active) < self.runner.cfg.concurrency.movies:
                    resources.check()
                    runner = JobRunner(
                        self.runner.cfg, self.events.put, translator=translator,
                        runtime=self.runner.runtime, root=self.runner.root,
                        resources=resources, task_id=str(submitted + 1),
                    )
                    runner.executor.cleanup_empty_dirs = self.runner.executor.cleanup_empty_dirs
                    active[submitted] = (runner, pool.submit(prepare, runner, movies[submitted]))
                    submitted += 1
                runner, future = active[index]
                while not future.done():
                    self._events()
                    resources.check()
                    wait([future], timeout=0.05)
                self._events()
                resources.check()
                result = future.result()
                if isinstance(result, PreparedJob):
                    conflicts = targets & result.output.targets
                    if conflicts:
                        runner.executor.stage = Stage.COMMIT
                        batch.warnings.extend(runner.executor.discard(result.output))
                        job = runner._failure(movies[index], FileExistsError("批次目标冲突: " + ", ".join(sorted(conflicts))),
                                              Stage.COMMIT, False)
                    else:
                        # 提交回调直接在主线程执行，允许在提交前取消。
                        runner.on_event = self.runner.on_event
                        job = runner.commit(result)
                        if job.success:
                            targets.update(result.output.targets)
                else:
                    job = result
                if job.error and isinstance(job.error.cause, BatchCancelled):
                    raise job.error.cause
                record(index, job)
                del active[index]
                self._events()
                if batch.aborted:
                    break
        except BaseException as cause:
            batch.aborted = True
            resources.stopped.set()
            batch.error = JobError("批次", Stage.PREPARE, cause)
        finally:
            if batch.aborted:
                resources.stopped.set()
                for _, future in active.values():
                    future.cancel()
            try:
                finish_executor(pool, [future for _, future in active.values()])
            except (KeyboardInterrupt, SystemExit) as cause:
                batch.aborted = True
                resources.stopped.set()
                batch.error = JobError("批次", Stage.PREPARE, cause)
            # 影片线程已结束，才能关闭子任务池、清理准备结果和模型资源。
            try:
                resources.close()
            except (KeyboardInterrupt, SystemExit) as cause:
                batch.aborted = True
                batch.error = JobError("批次", Stage.PREPARE, cause)
            for index, (runner, future) in active.items():
                if index in completed or future.cancelled():
                    continue
                try:
                    result = future.result()
                    if isinstance(result, PreparedJob):
                        if runner.executor.committed_result is not None:
                            record(index, runner._failure(movies[index], batch.error.cause, Stage.COMMIT, True))
                        else:
                            batch.warnings.extend(runner.executor.discard(result.output))
                    elif not (result.error and isinstance(result.error.cause, BatchCancelled)):
                        record(index, result)
                except (KeyboardInterrupt, SystemExit) as cause:
                    batch.aborted = True
                    batch.error = JobError("批次", Stage.PREPARE, cause)
                except BaseException as cause:
                    record(index, JobResult(movies[index], error=JobError(movies[index].dvdid or movies[index].cid,
                                                                         Stage.PREPARE, cause)))
            try:
                self._events()
            except (KeyboardInterrupt, SystemExit) as cause:
                batch.aborted = True
                batch.error = JobError("批次", Stage.PREPARE, cause)
        batch.jobs = [completed[index] for index in sorted(completed)]
        batch.pending = [movie for index, movie in enumerate(movies) if index not in completed]
        return batch
