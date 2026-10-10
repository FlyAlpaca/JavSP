"""只读输出规划和单片输出事务；不提供进程崩溃后的自动恢复。"""

import errno
import json
import logging
import os
import re
import shutil
import tempfile
from copy import copy
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from javsp.avid import get_id, normalize_id
from javsp.datatype import Movie, MovieInfo, filemove_logger
from javsp.image import valid_pic
from javsp.models import OutputResult, Stage, StageEvent
from javsp.nfo import write_nfo
from javsp.processor import download_cover, download_extra_fanarts, generate_names, process_poster

logger = logging.getLogger(__name__)


def _stamp(path):
    """lstat 同时识别替换、内容修改和符号链接；不存在返回 None。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    return st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def _snapshot(movie, info=None):
    result = copy(movie)
    result.files = [os.path.abspath(path) for path in movie.files]
    selected = info if info is not None else movie.info
    result.info = selected.model_copy(deep=True) if selected is not None else None
    return result


@dataclass(frozen=True)
class FileOperation:
    source: str
    target: str
    source_stamp: tuple
    hardlink: bool = False
    skip: bool = False


@dataclass(frozen=True)
class OutputPlan:
    movie: Movie
    operations: tuple[FileOperation, ...]
    artifacts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    generate: bool = True


def _file_operations(movie, cfg, hardlink):
    operations, warnings, occupied = [], [], set()
    for index, source in enumerate(movie.files, 1):
        suffix = f"-CD{index}" if len(movie.files) > 1 else ""
        target = os.path.abspath(os.path.join(movie.save_dir, movie.basename + suffix + os.path.splitext(source)[1]))
        if os.path.lexists(target) or _key(target) in occupied:
            raise FileExistsError(f"File exists: {target}")
        occupied.add(_key(target))
        stamp = _stamp(source)
        if stamp is None or not os.path.isfile(source) or os.path.islink(source):
            raise ValueError(f"影片源文件不存在或不是普通文件: {source}")
        operations.append(FileOperation(source, target, stamp, hardlink))
    if cfg.summarizer.match_subtitles and movie.files:
        identity = normalize_id(movie.dvdid)
        parent = Path(movie.files[0]).parent
        subtitles = []
        if identity:
            for source in sorted(parent.iterdir()):
                if source.is_file() and source.suffix.lower() in (".srt", ".ass"):
                    if normalize_id(get_id(source.stem)) == identity:
                        subtitles.append(source)
        if len(movie.files) > 1 and len(subtitles) not in (0, len(movie.files)):
            warnings.append(f"多CD视频({len(movie.files)}个)与匹配字幕({len(subtitles)}个)数量不一致，仍将一起移动")
        for source in subtitles:
            suffix = ""
            if len(movie.files) > 1:
                match = re.search(r"cd(\d+)$", source.stem, re.I)
                if match:
                    suffix = f"-CD{match[1]}"
            target = os.path.abspath(os.path.join(movie.save_dir, movie.basename + suffix + source.suffix.lower()))
            skip = os.path.lexists(target) or _key(target) in occupied
            if skip:
                warnings.append(f"字幕文件已存在，跳过: '{target}'")
            occupied.add(_key(target))
            if source.is_symlink() and not skip:
                raise ValueError(f"字幕源文件不是普通文件: {source}")
            operations.append(FileOperation(str(source), target, _stamp(source), hardlink, skip))
    return tuple(operations), tuple(warnings)


def _validate_targets(operations, artifacts, sources=()):
    targets = {_key(op.target) for op in operations if not op.skip}
    source_keys = {_key(path) for path in sources}
    for target in artifacts:
        key = _key(target)
        if key in targets or key in source_keys:
            raise ValueError(f"输出目标重叠: {target}")
        targets.add(key)
    # 一个操作的文件目标不能同时成为另一操作的父目录。
    for target in targets:
        if any(_key(parent) in targets for parent in Path(target).parents):
            raise ValueError(f"输出文件与目录重叠: {target}")


class OutputPlanner:
    def __init__(self, cfg):
        self.cfg = cfg

    def plan(self, movie: Movie, info: MovieInfo) -> OutputPlan:
        planned = _snapshot(movie, info)
        generate_names(planned, self.cfg)
        if not planned.save_dir:
            raise ValueError("无法按命名规则生成目标文件夹")
        for attr in ("save_dir", "nfo_file", "fanart_file", "poster_file"):
            setattr(planned, attr, os.path.abspath(getattr(planned, attr)))
        if self.cfg.summarizer.move_files:
            operations, warnings = _file_operations(planned, self.cfg, self.cfg.summarizer.path.hard_link)
        else:
            operations, warnings = (), ()
        artifacts = [planned.fanart_file, planned.poster_file, planned.nfo_file]
        folder = os.path.join(planned.save_dir, "folder.jpg")
        if _key(folder) not in {_key(planned.fanart_file), _key(planned.poster_file)}:
            artifacts.append(folder)
        _validate_targets(operations, artifacts, planned.files)
        for path in artifacts:
            if os.path.lexists(path) and (not os.path.isfile(path) or os.path.islink(path)):
                raise ValueError(f"产物目标不是普通文件: {path}")
        return OutputPlan(planned, operations, tuple(artifacts), warnings)


@dataclass
class _Entry:
    target: str
    source: str | None = None
    backup: str | None = None
    stamp: tuple | None = None
    backup_stamp: tuple | None = None
    installed: bool = False
    source_removed: bool = False
    hardlink: bool = False
    source_stamp: tuple | None = None


class RecoveryError(RuntimeError):
    """保留恢复目录，禁止随后任务继续覆盖可能需要恢复的路径。"""

    def __init__(self, cause, errors, paths):
        self.cause, self.paths = cause, paths
        super().__init__("输出失败且回滚不完整: " + "; ".join(errors) + " | 恢复清单: " + ", ".join(paths))


class _Transaction:
    def __init__(self):
        self.entries: list[_Entry] = []
        self.workspaces: dict[str, str] = {}
        self.created_dirs: list[str] = []

    def temp(self, target, suffix=""):
        parent = Path(target).parent
        while not parent.is_dir():
            if parent == parent.parent:
                raise OSError(f"找不到目标文件系统: {target}")
            parent = parent.parent
        key = str(parent)
        if key not in self.workspaces:
            self.workspaces[key] = tempfile.mkdtemp(prefix=".javsp-", dir=parent)
        fd, path = tempfile.mkstemp(dir=self.workspaces[key], suffix=suffix)
        os.close(fd)
        return path

    def mkdir(self, parent):
        missing = []
        current = Path(parent)
        while not current.exists():
            missing.append(current)
            current = current.parent
        for path in reversed(missing):
            path.mkdir()
            self.created_dirs.append(str(path))

    @staticmethod
    def check(path, expected):
        if _stamp(path) != expected:
            raise OSError(f"文件在规划/准备后被修改: {path}")

    def move(self, operation):
        self.check(operation.source, operation.source_stamp)
        if os.path.lexists(operation.target):
            raise FileExistsError(f"File exists: {operation.target}")
        self.mkdir(Path(operation.target).parent)
        entry = _Entry(operation.target, operation.source, hardlink=operation.hardlink,
                       source_stamp=operation.source_stamp)
        self.entries.append(entry)
        if operation.hardlink:
            try:
                os.link(operation.source, operation.target)
                entry.installed = True
                entry.stamp = _stamp(operation.target)
                entry.source_stamp = _stamp(operation.source)
                return
            except OSError as error:
                if os.path.lexists(operation.target):
                    raise error
                logger.warning(f"创建硬链接失败，回退为复制文件: '{operation.source}' -> '{operation.target}'")
        else:
            try:
                os.rename(operation.source, operation.target)
                entry.installed, entry.source_removed = True, True
                entry.stamp = _stamp(operation.target)
                return
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
        staged = self.temp(operation.target)
        shutil.copy2(operation.source, staged)
        self.check(operation.source, operation.source_stamp)
        if os.path.getsize(staged) != operation.source_stamp[3]:
            raise OSError(f"跨设备复制大小校验失败: {operation.source}")
        if os.path.lexists(operation.target):
            raise FileExistsError(f"File exists: {operation.target}")
        os.rename(staged, operation.target)
        entry.installed, entry.stamp = True, _stamp(operation.target)
        if not operation.hardlink:
            os.unlink(operation.source)
            entry.source_removed = True

    def publish(self, staged, target, expected):
        self.check(target, expected)
        self.mkdir(Path(target).parent)
        entry = _Entry(target)
        self.entries.append(entry)
        if expected is not None:
            backup = self.temp(target)
            os.replace(target, backup)
            entry.backup, entry.backup_stamp = backup, _stamp(backup)
        os.replace(staged, target)
        entry.installed, entry.stamp = True, _stamp(target)

    def rollback(self):
        errors = []
        for entry in reversed(self.entries):
            try:
                if entry.installed:
                    self.check(entry.target, entry.stamp)
                    if entry.source and entry.source_removed:
                        if os.path.lexists(entry.source):
                            raise FileExistsError(f"回滚源路径已存在: {entry.source}")
                        # 回跨设备时先复制验证，再删除可恢复的目标副本。
                        try:
                            os.rename(entry.target, entry.source)
                        except OSError as error:
                            if error.errno != errno.EXDEV:
                                raise
                            staged = self.temp(entry.source)
                            shutil.copy2(entry.target, staged)
                            self.check(entry.target, entry.stamp)
                            if os.path.getsize(staged) != entry.stamp[3]:
                                raise OSError(f"回滚复制大小校验失败: {entry.source}")
                            if os.path.lexists(entry.source):
                                raise FileExistsError(f"回滚源路径已存在: {entry.source}")
                            os.rename(staged, entry.source)
                            os.unlink(entry.target)
                    else:
                        if entry.source:
                            self.check(entry.source, entry.source_stamp)
                        os.unlink(entry.target)
                if entry.backup:
                    self.check(entry.backup, entry.backup_stamp)
                    if os.path.lexists(entry.target):
                        raise FileExistsError(f"回滚目标路径已存在: {entry.target}")
                    os.replace(entry.backup, entry.target)
            except (OSError, ValueError) as error:
                errors.append(f"{entry.target} -> {entry.source or entry.backup}: {error}")
        return errors

    def preserve(self, cause, errors):
        payload = {"cause": repr(cause), "errors": errors, "entries": [vars(entry) for entry in self.entries],
                   "created_dirs": self.created_dirs, "workspaces": list(self.workspaces.values())}
        paths = list(self.workspaces.values())
        if not paths and self.entries:
            self.temp(self.entries[0].target)
            paths = list(self.workspaces.values())
        for workspace in paths:
            try:
                path = os.path.join(workspace, "recovery.json")
                with open(path, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False, indent=2)
            except OSError as error:
                logger.error(f"无法写入恢复清单 {workspace}: {error}")
        return paths

    def cleanup(self, remove_dirs=False):
        warnings = []
        for path in self.workspaces.values():
            try:
                shutil.rmtree(path)
            except OSError as error:
                warnings.append(f"清理临时目录失败，保留路径 {path}: {error}")
        if remove_dirs:
            for path in reversed(self.created_dirs):
                try:
                    os.rmdir(path)
                except OSError:
                    pass
        return warnings


class OutputExecutor:
    def __init__(self, cfg, on_event=None):
        self.cfg, self.on_event = cfg, on_event
        self.stage = Stage.PREPARE

    def _emit(self, movie, stage):
        self.stage = stage
        if self.on_event:
            try:
                self.on_event(StageEvent(movie.dvdid or movie.cid or "未知番号", stage))
            except Exception:
                logger.warning("进度回调失败", exc_info=True)

    def _prepare(self, plan, tx):
        movie = _snapshot(plan.movie)
        artifacts = []
        fanart = tx.temp(movie.fanart_file, ".jpg")
        result = download_cover(movie.info.covers, fanart,
                                movie.info.big_covers if self.cfg.summarizer.cover.highres else None,
                                movie_id=movie.dvdid or movie.cid, cfg=self.cfg)
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
        process_poster(staged_movie, self.cfg, folder, generate_folder)
        artifacts.append((staged_movie.poster_file, movie.poster_file))
        if folder:
            artifacts.append((folder, folder_target))
        if self.cfg.summarizer.extra_fanarts.enabled:
            extra = tx.temp(os.path.join(movie.save_dir, "extrafanart", "0.jpg"))
            os.unlink(extra)
            os.mkdir(extra)
            download_extra_fanarts(staged_movie, self.cfg.summarizer.extra_fanarts.scrap_interval.total_seconds(),
                                   destination_dir=extra)
            artifacts.extend((str(path), os.path.join(movie.save_dir, "extrafanart", path.name))
                             for path in sorted(Path(extra).iterdir()))
        nfo = tx.temp(movie.nfo_file, ".nfo")
        write_nfo(movie.info, nfo, self.cfg)
        artifacts.append((nfo, movie.nfo_file))
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
        if source:
            movie.info.field_sources = dict(movie.info.field_sources or {}, cover=source)
        elif movie.info.cover != plan.movie.info.cover and movie.info.field_sources:
            movie.info.field_sources.pop("cover", None)
        return movie, [(staged, target, _stamp(target)) for staged, target in artifacts]

    def execute_files(self, movie, hardlink=False):
        planned = _snapshot(movie)
        operations, warnings = _file_operations(planned, self.cfg, hardlink)
        return self.execute(OutputPlan(planned, operations, warnings=warnings, generate=False))

    def execute(self, plan: OutputPlan) -> OutputResult:
        tx = _Transaction()
        movie = _snapshot(plan.movie)
        warnings = list(plan.warnings)
        for warning in warnings:
            logger.warning(warning)
        try:
            self._emit(movie, Stage.PREPARE)
            artifacts = []
            if plan.generate:
                movie, artifacts = self._prepare(plan, tx)
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
        except BaseException as cause:
            failed_stage = self.stage
            self._emit(movie, Stage.ROLLBACK)
            errors = tx.rollback()
            self.stage = failed_stage
            if errors:
                paths = tx.preserve(cause, errors)
                raise RecoveryError(cause, errors, paths) from cause
            tx.cleanup(remove_dirs=True)
            raise
        # 到这里整片已提交；清理/日志错误不能再把已提交任务误判为可回滚。
        warnings.extend(tx.cleanup())
        moved = [(op.source, op.target) for op in plan.operations if not op.skip]
        if moved:
            movie.files = [op.target for op in plan.operations if not op.skip and op.source in plan.movie.files]
        for source, target in moved:
            logger.info(f"重命名文件: '{source}' -> '{target}'")
            filemove_logger.debug(f'移动（重命名）文件: \n  原路径: "{source}"\n  新路径: "{target}"')
        for parent in {str(Path(source).parent) for source, _ in moved}:
            try:
                os.rmdir(parent)
            except OSError:
                pass
        return OutputResult(movie, moved, [target for _, target, _ in artifacts], warnings)
