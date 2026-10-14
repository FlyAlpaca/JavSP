"""协调产物准备与整片文件事务。"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from javsp.datatype import Movie, filemove_logger
from javsp.publish.image import valid_pic
from javsp.publish.media import (
    actor_image_path,
    dmm_cover_candidates,
    download_actor,
    download_cover,
    download_extra_fanarts,
    process_poster,
)
from javsp.publish.naming import validate_output_paths
from javsp.publish.nfo import write_nfo
from javsp.publish.planner import OutputPlan, _file_operations, _snapshot, _validate_targets
from javsp.publish.transaction import FileTransaction, RecoveryError, _key, _stamp
from javsp.workflow.models import OutputResult, Stage, StageEvent

logger = logging.getLogger(__name__)


@dataclass
class PreparedOutput:
    plan: OutputPlan
    movie: Movie
    transaction: FileTransaction
    artifacts: list[tuple[str, str, tuple[int, ...] | None]]
    warnings: list[str]
    finished: bool = False

    @property
    def targets(self):
        return {_key(op.target) for op in self.plan.operations if not op.skip} | {
            _key(target) for _, target, _ in self.artifacts
        }


class OutputExecutor:
    def __init__(self, cfg, on_event=None, *, cleanup_empty_dirs=True, resources=None):
        self.cfg, self.on_event = cfg, on_event
        self.cleanup_empty_dirs = cleanup_empty_dirs
        self.resources = resources
        self.stage = Stage.PREPARE
        self.committed_result = None

    def _emit(self, movie, stage):
        self.stage = stage
        if self.on_event:
            try:
                self.on_event(StageEvent(movie.dvdid or movie.cid or "未知番号", stage))
            except BaseException:
                if stage != Stage.ROLLBACK:
                    raise
                logger.warning("恢复进度回调失败，继续恢复文件", exc_info=True)

    def _prepare(self, plan, tx):
        movie = _snapshot(plan.movie)
        artifacts, warnings = [], []
        fanart = tx.temp(movie.fanart_file, ".jpg")
        result = download_cover(movie.info.covers, fanart,
                                movie.info.big_covers if self.cfg.summarizer.cover.highres else None,
                                movie_id=movie.dvdid or movie.cid, cfg=self.cfg, resources=self.resources)
        if not result or result[0] is None:
            raise ValueError(f"下载封面图片失败: {result[1] if result else '未知原因'}")
        movie.info.cover, fanart = result
        extension = os.path.splitext(fanart)[1]
        movie.fanart_file = os.path.splitext(movie.fanart_file)[0] + extension
        movie.poster_file = os.path.splitext(movie.poster_file)[0] + extension
        artifacts.append((fanart, movie.fanart_file))
        staged_movie = _snapshot(movie)
        staged_movie.fanart_file = fanart
        staged_movie.poster_file = tx.temp(movie.poster_file, extension)
        folder_target = os.path.join(movie.save_dir, "folder.jpg")
        generate_folder = _key(folder_target) not in {_key(movie.fanart_file), _key(movie.poster_file)}
        folder = tx.temp(folder_target, ".jpg") if generate_folder else None
        portrait_path = None
        dmm = dmm_cover_candidates(plan.movie.info.covers, plan.movie.info.big_covers)
        if dmm and self.cfg.summarizer.cover.highres:
            portrait = tx.temp(movie.poster_file, ".jpg")
            portrait_result = download_cover([], portrait, [dmm[3]], movie_id=movie.dvdid or movie.cid,
                                             cfg=self.cfg, resources=self.resources, optional=True)
            if portrait_result[0]:
                portrait_path = portrait_result[1]
            else:
                warnings.append("竖版原图不可用，已从横版封面裁剪海报")
        process_poster(staged_movie, self.cfg, folder, generate_folder, portrait_path=portrait_path)
        artifacts.append((staged_movie.poster_file, movie.poster_file))
        if folder:
            artifacts.append((folder, folder_target))
        if self.cfg.summarizer.extra_fanarts.enabled:
            extra = tx.temp(os.path.join(movie.save_dir, "extrafanart", "0.jpg"))
            os.unlink(extra)
            os.mkdir(extra)
            warnings.extend(download_extra_fanarts(
                staged_movie, self.cfg.summarizer.extra_fanarts.scrap_interval.total_seconds(),
                destination_dir=extra, cfg=self.cfg, resources=self.resources
            ))
            artifacts.extend((str(path), os.path.join(movie.save_dir, "extrafanart", path.name))
                             for path in sorted(Path(extra).iterdir()))
        for name in movie.info.actress or []:
            url = (movie.info.actress_pics or {}).get(name)
            if not url:
                continue
            target = actor_image_path(movie.save_dir, name)
            staged = tx.temp(target, ".jpg")
            try:
                download_actor(url, staged, cfg=self.cfg, resources=self.resources)
            except Exception as error:
                warnings.append(f"女优头像不可用: {name} ({type(error).__name__})")
                continue
            artifacts.append((staged, target))
            # 绿联影视中心通过 thumb URL 下载头像，不能用本地缓存路径覆盖远程地址。
            if not url.lower().startswith(("http://", "https://")):
                movie.info.actress_pics[name] = os.path.relpath(target, movie.save_dir).replace(os.sep, "/")
        nfo = tx.temp(movie.nfo_file, ".nfo")
        write_nfo(movie.info, nfo, self.cfg)
        artifacts.append((nfo, movie.nfo_file))
        validate_output_paths([target for _, target in artifacts], self.cfg)
        _validate_targets(plan.operations, [target for _, target in artifacts], movie.files)
        for staged, target in artifacts:
            if target == movie.nfo_file:
                etree.parse(staged)
            elif not valid_pic(staged):
                raise ValueError(f"产物图片校验失败: {target}")
            if os.path.lexists(target) and (not os.path.isfile(target) or os.path.islink(target)):
                raise ValueError(f"产物目标不是普通文件: {target}")
        # 保留下载获胜封面的实际来源。
        source = movie.info._cover_sources.get(movie.info.cover)
        if not source and dmm:
            source = next((plan.movie.info._cover_sources.get(url) for url in
                           [*(plan.movie.info.big_covers or []), *(plan.movie.info.covers or [])]
                           if plan.movie.info._cover_sources.get(url)), None)
        if source:
            movie.info.field_sources = dict(movie.info.field_sources or {}, cover=source)
        elif movie.info.cover != plan.movie.info.cover and movie.info.field_sources:
            movie.info.field_sources.pop("cover", None)
        return movie, [(staged, target, _stamp(target)) for staged, target in artifacts], warnings

    def execute_files(self, movie, hardlink=False):
        planned = _snapshot(movie)
        operations, warnings = _file_operations(planned, self.cfg, hardlink)
        return self.execute(OutputPlan(planned, operations, warnings=warnings, generate=False))

    def _recover(self, prepared, cause):
        failed_stage = self.stage
        self._emit(prepared.movie, Stage.ROLLBACK)
        errors = prepared.transaction.rollback()
        self.stage = failed_stage
        prepared.finished = True
        if errors:
            paths = prepared.transaction.preserve(cause, errors)
            raise RecoveryError(cause, errors, paths) from cause
        for warning in prepared.transaction.cleanup(remove_dirs=True):
            logger.warning(warning)

    def discard(self, prepared):
        if prepared.finished:
            return []
        interruption = None
        while True:
            try:
                warnings = prepared.transaction.cleanup(remove_dirs=True)
                break
            except (KeyboardInterrupt, SystemExit) as cause:
                interruption = cause
        prepared.finished = True
        if interruption:
            for warning in warnings:
                logger.warning(warning)
            raise interruption
        return warnings

    def prepare(self, plan: OutputPlan) -> PreparedOutput:
        self.committed_result = None
        validate_output_paths([op.target for op in plan.operations if not op.skip] + list(plan.artifacts), self.cfg)
        prepared = PreparedOutput(plan, _snapshot(plan.movie), FileTransaction(), [], list(plan.warnings))
        for warning in prepared.warnings:
            logger.warning(warning)
        try:
            if self.resources:
                self.resources.check()
            self._emit(prepared.movie, Stage.PREPARE)
            if plan.generate:
                prepared.movie, prepared.artifacts, warnings = self._prepare(plan, prepared.transaction)
                prepared.warnings.extend(warnings)
            if self.resources:
                self.resources.check()
            return prepared
        except BaseException as cause:
            self._recover(prepared, cause)
            raise

    def execute(self, plan: OutputPlan) -> OutputResult:
        return self.commit(self.prepare(plan))

    def commit(self, prepared: PreparedOutput) -> OutputResult:
        if prepared.finished:
            raise ValueError("准备结果已提交或已放弃")
        plan, movie, tx = prepared.plan, prepared.movie, prepared.transaction
        artifacts, warnings = prepared.artifacts, prepared.warnings
        self.committed_result = None
        try:
            if self.resources:
                self.resources.check()
            moved = [(op.source, op.target) for op in plan.operations if not op.skip]
            if moved:
                movie.files = [op.target for op in plan.operations if not op.skip and op.source in plan.movie.files]
            self._emit(movie, Stage.COMMIT)
            for operation in plan.operations:
                if not operation.skip:
                    tx.check(operation.source, operation.source_stamp)
                    if os.path.lexists(operation.target):
                        raise FileExistsError(f"File exists: {operation.target}")
            for _, target, stamp in artifacts:
                tx.check(target, stamp)
            for operation in plan.operations:
                if not operation.skip:
                    tx.move(operation)
            for staged, target, stamp in artifacts:
                tx.publish(staged, target, stamp)
            self.committed_result = OutputResult(
                movie, moved, [target for _, target, _ in artifacts], warnings,
                [op.source for op in plan.operations if op.skip],
            )
        except BaseException as cause:
            self._recover(prepared, cause)
            raise
        prepared.finished = True
        # 到这里整片已提交；清理/日志错误不能再把已提交任务误判为可回滚。
        cleanup_warnings = tx.cleanup()
        warnings.extend(cleanup_warnings)
        for warning in cleanup_warnings:
            logger.warning(warning)
        for source, target in moved:
            logger.debug(f"重命名文件: '{source}' -> '{target}'")
            filemove_logger.debug(f'移动（重命名）文件: \n  原路径: "{source}"\n  新路径: "{target}"')
        parents = {str(Path(source).parent) for source, _ in moved} if self.cleanup_empty_dirs else set()
        for parent in parents:
            try:
                os.rmdir(parent)
            except OSError:
                pass
        return self.committed_result
