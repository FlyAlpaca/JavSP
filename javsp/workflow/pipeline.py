"""逐部影片流水线：统一编排、阶段事件、任务错误和批次结果。"""

import logging
from copy import copy

from javsp.crawl.dispatcher import CrawlerExecutor
from javsp.metadata.summarizer import MetadataMerger
from javsp.publish.output import OutputExecutor
from javsp.publish.planner import OutputPlanner
from javsp.publish.transaction import RecoveryError
from javsp.scan.file import Scanner
from javsp.workflow.models import BatchResult, JobError, JobResult, Stage, StageEvent

logger = logging.getLogger(__name__)


class _CrawlerProgress:
    def __init__(self, emit):
        self.emit = emit

    def set_description(self, detail):
        self.emit(Stage.CRAWL, detail)


class JobRunner:
    def __init__(self, cfg, on_event=None, *, translator=None, crawler=None, merger=None, planner=None,
                 executor=None, runtime=None, root=None):
        self.cfg, self.on_event = cfg, on_event
        self.runtime = runtime
        self.translator = translator if translator is not None else (runtime.translator if runtime else None)
        self.crawler = crawler if crawler is not None else CrawlerExecutor(cfg, runtime=runtime)
        self.merger = merger if merger is not None else MetadataMerger(cfg, runtime=runtime)
        self.planner = planner if planner is not None else OutputPlanner(cfg, root=root or (runtime.root if runtime else None))
        self.executor = executor if executor is not None else OutputExecutor(cfg)

    def _event(self, event):
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                logger.warning("进度回调失败", exc_info=True)

    def run(self, movie) -> JobResult:
        if self.translator is None:
            raise ValueError("翻译器未初始化，请传入翻译器或已准备的运行上下文")
        working = copy(movie)
        working.files = list(movie.files)
        stage = Stage.CRAWL
        executing = False

        def emit(next_stage, detail=""):
            nonlocal stage
            stage = next_stage
            self._event(StageEvent(working.dvdid or working.cid or "未知番号", stage, detail))

        self.executor.on_event = self._event
        try:
            emit(Stage.CRAWL)
            result = self.crawler.crawl(working, _CrawlerProgress(emit))
            working.dvdid, working.cid, working.data_src = result.dvdid, result.cid, result.data_src
            result.require_data()
            if result.failures:
                logger.info("部分抓取器失败: " + ", ".join(sorted({name for name, _ in result.failures})))
            emit(Stage.MERGE)
            working.info = self.merger.merge(working, result)
            from javsp.translate import translate_movie_info

            emit(Stage.TRANSLATE)
            translate_movie_info(working.info, self.translator)
            emit(Stage.PLAN)
            plan = self.planner.plan(working, working.info)
            stage = Stage.PREPARE
            executing = True
            output = self.executor.execute(plan)
            # 只有提交成功，才对外发布影片的新路径和最终元数据。
            movie.__dict__.update(output.movie.__dict__)
            output.movie = movie
            emit(Stage.DONE)
            logger.info(f"整理完成，相关文件已保存到: {movie.save_dir}\n")
            return JobResult(movie, output)
        except BaseException as cause:
            committed = getattr(self.executor, "committed_result", None) if executing else None
            if committed is not None:
                # 提交点之后的取消或清理错误不能把已经完成的影片计为失败。
                movie.__dict__.update(committed.movie.__dict__)
                committed.movie = movie
                cancelled = isinstance(cause, (KeyboardInterrupt, SystemExit, GeneratorExit))
                if not cancelled:
                    warning = f"产物已提交，后续清理或通知异常: {cause}"
                    committed.warnings.append(warning)
                    logger.warning(warning)
                return JobResult(movie, committed, cancelled=cancelled, interruption=cause if cancelled else None)
            if executing:
                stage = self.executor.stage
            recovered = not isinstance(cause, RecoveryError)
            original = cause.cause if not recovered else cause
            cancelled = isinstance(original, (KeyboardInterrupt, SystemExit, GeneratorExit))
            error = JobError(
                working.dvdid or working.cid or "未知番号", stage, cause, recovered, cause.paths if not recovered else []
            )
            logger.error(f"整理失败: {error}")
            logger.debug("单片任务异常", exc_info=True)
            return JobResult(movie, error=error, cancelled=cancelled)

    def run_batch(self, movies, on_result=None) -> BatchResult:
        movies = list(movies)
        batch = BatchResult(len(movies))
        for index, movie in enumerate(movies):
            job = self.run(movie)
            batch.jobs.append(job)
            if on_result:
                try:
                    on_result(job)
                except Exception:
                    logger.warning("任务结果回调失败", exc_info=True)
            if job.cancelled or (job.error and not job.error.recovered):
                batch.aborted = True
                batch.pending = movies[index + 1 :]
                if job.interruption:
                    batch.error = JobError(movie.dvdid or movie.cid or "未知番号", Stage.DONE, job.interruption)
                break
            if job.success and index + 1 < len(movies):
                try:
                    self.crawler.throttle()
                except BaseException as cause:
                    batch.aborted = True
                    batch.pending = movies[index + 1 :]
                    batch.error = JobError(movie.dvdid or movie.cid or "未知番号", Stage.CRAWL, cause)
                    break
        return batch


class Pipeline:
    def __init__(self, cfg, on_event=None, translator=None, *, runtime=None):
        self.cfg, self.on_event = cfg, on_event
        self.runtime = runtime
        self.translator = translator if translator is not None else (runtime.translator if runtime else None)

    def scan(self, root):
        if self.on_event:
            try:
                self.on_event(StageEvent("", Stage.SCAN, str(root)))
            except Exception:
                logger.warning("进度回调失败", exc_info=True)
        return Scanner(self.cfg).scan(root)

    def run(self, root, review=None, on_result=None) -> BatchResult:
        scan = self.scan(root)
        if review:
            review(scan.movies, root)
        runner = JobRunner(self.cfg, self.on_event, translator=self.translator, runtime=self.runtime, root=root)
        batch = runner.run_batch(scan.movies, on_result)
        batch.scan = scan
        return batch
