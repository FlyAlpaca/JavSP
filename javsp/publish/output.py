"""只读输出规划和单片输出事务；不提供进程崩溃后的自动恢复。"""

import ctypes
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

from javsp.datatype import Movie, MovieInfo, filemove_logger
from javsp.publish.image import valid_pic
from javsp.publish.nfo import write_nfo
from javsp.publish.processor import download_cover, download_extra_fanarts, generate_names, process_poster
from javsp.scan.avid import get_id, normalize_id
from javsp.workflow.models import OutputResult, Stage, StageEvent

logger = logging.getLogger(__name__)


def _rename_new(source, target):
    """发布普通文件，原子拒绝已有目标；Linux 使用 RENAME_NOREPLACE。"""
    if os.name == "nt":
        # Windows 的 rename 原生拒绝已有目标。
        os.rename(source, target)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is not None:
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) == 0:
            return
        error = ctypes.get_errno()
        if error not in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
            raise OSError(error, os.strerror(error), str(target))
    # 缺少不覆盖重命名的平台：link 原子拒绝已有目标，再删除源名称。
    os.link(source, target)
    os.unlink(source)


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
                    if normalize_id(get_id(source.stem, cfg)) == identity:
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
    incoming_stamp: tuple | None = None
    backup_origin_stamp: tuple | None = None


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
                       source_stamp=operation.source_stamp, incoming_stamp=operation.source_stamp)
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
                _rename_new(operation.source, operation.target)
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
        entry.incoming_stamp = _stamp(staged)
        _rename_new(staged, operation.target)
        entry.installed, entry.stamp = True, _stamp(operation.target)
        if not operation.hardlink:
            self.check(operation.source, operation.source_stamp)
            os.unlink(operation.source)
            entry.source_removed = True

    def publish(self, staged, target, expected):
        self.check(target, expected)
        self.mkdir(Path(target).parent)
        entry = _Entry(target, incoming_stamp=_stamp(staged))
        self.entries.append(entry)
        if expected is not None:
            backup = self.temp(target)
            entry.backup, entry.backup_origin_stamp = backup, expected
            os.replace(target, backup)
            entry.backup_stamp = _stamp(backup)
            if not self._same_file(entry.backup_stamp, expected):
                raise OSError(f"产物在备份前被修改: {target}")
        _rename_new(staged, target)
        entry.installed, entry.stamp = True, _stamp(target)

    @staticmethod
    def _same_file(current, expected):
        # rename/link 会改变 ctime；其余信息识别尚未登记完成的原子文件操作。
        return current is not None and expected is not None and current[:5] == expected[:5]

    def _reconcile(self, entry):
        """信号可能在系统调用成功后、登记完成前到达，从文件身份补全状态。"""
        current = _stamp(entry.target)
        if not entry.installed and self._same_file(current, entry.incoming_stamp):
            entry.installed, entry.stamp = True, current
            if entry.source and self._same_file(_stamp(entry.source), entry.source_stamp):
                entry.source_stamp = _stamp(entry.source)
        if entry.source and entry.installed and not os.path.lexists(entry.source):
            entry.source_removed = True
        if entry.backup and entry.backup_stamp is None:
            backup_stamp = _stamp(entry.backup)
            if self._same_file(backup_stamp, entry.backup_origin_stamp):
                entry.backup_stamp = backup_stamp
            elif not entry.installed and current == entry.backup_origin_stamp:
                # 备份移动未发生，旧目标仍在原位置；空占位文件只需清理。
                entry.backup = None
            else:
                raise OSError(f"备份操作状态不明，保留文件: {entry.backup}")

    def rollback(self):
        errors = []
        for entry in reversed(self.entries):
            try:
                self._reconcile(entry)
                if entry.installed:
                    self.check(entry.target, entry.stamp)
                    if entry.source and entry.source_removed:
                        if os.path.lexists(entry.source):
                            raise FileExistsError(f"回滚源路径已存在: {entry.source}")
                        # 回跨设备时先复制验证，再删除可恢复的目标副本。
                        try:
                            _rename_new(entry.target, entry.source)
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
                            _rename_new(staged, entry.source)
                            os.unlink(entry.target)
                        entry.source_removed = False
                    else:
                        if entry.source:
                            self.check(entry.source, entry.source_stamp)
                        os.unlink(entry.target)
                    entry.installed = False
                if entry.backup:
                    self.check(entry.backup, entry.backup_stamp)
                    if os.path.lexists(entry.target):
                        raise FileExistsError(f"回滚目标路径已存在: {entry.target}")
                    _rename_new(entry.backup, entry.target)
                    entry.backup = None
                    entry.backup_stamp = None
            except BaseException as error:
                errors.append(f"{entry.target} -> {entry.source or entry.backup}: {error}")
        return errors

    def preserve(self, cause, errors):
        paths = list(self.workspaces.values())
        if not paths and self.entries:
            self.temp(self.entries[0].target)
            paths = list(self.workspaces.values())
        payload = {"cause": repr(cause), "errors": errors, "entries": [vars(entry) for entry in self.entries],
                   "created_dirs": self.created_dirs, "workspaces": paths}
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
            warnings.extend(download_extra_fanarts(
                staged_movie, self.cfg.summarizer.extra_fanarts.scrap_interval.total_seconds(), destination_dir=extra
            ))
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
        return movie, [(staged, target, _stamp(target)) for staged, target in artifacts], warnings

    def execute_files(self, movie, hardlink=False):
        planned = _snapshot(movie)
        operations, warnings = _file_operations(planned, self.cfg, hardlink)
        return self.execute(OutputPlan(planned, operations, warnings=warnings, generate=False))

    def execute(self, plan: OutputPlan) -> OutputResult:
        tx = _Transaction()
        self.committed_result = None
        movie = _snapshot(plan.movie)
        warnings = list(plan.warnings)
        for warning in warnings:
            logger.warning(warning)
        try:
            self._emit(movie, Stage.PREPARE)
            artifacts = []
            if plan.generate:
                movie, artifacts, prepared_warnings = self._prepare(plan, tx)
                warnings.extend(prepared_warnings)
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
            self.committed_result = OutputResult(movie, moved, [target for _, target, _ in artifacts], warnings)
        except BaseException as cause:
            failed_stage = self.stage
            self._emit(movie, Stage.ROLLBACK)
            errors = tx.rollback()
            self.stage = failed_stage
            if errors:
                paths = tx.preserve(cause, errors)
                raise RecoveryError(cause, errors, paths) from cause
            for warning in tx.cleanup(remove_dirs=True):
                logger.warning(warning)
            raise
        # 到这里整片已提交；清理/日志错误不能再把已提交任务误判为可回滚。
        cleanup_warnings = tx.cleanup()
        warnings.extend(cleanup_warnings)
        for warning in cleanup_warnings:
            logger.warning(warning)
        for source, target in moved:
            logger.info(f"重命名文件: '{source}' -> '{target}'")
            filemove_logger.debug(f'移动（重命名）文件: \n  原路径: "{source}"\n  新路径: "{target}"')
        for parent in {str(Path(source).parent) for source, _ in moved}:
            try:
                os.rmdir(parent)
            except OSError:
                pass
        return self.committed_result
