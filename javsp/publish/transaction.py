"""文件事务、备份、逆序恢复及恢复清单。"""

import ctypes
import errno
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

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


class FileTransaction:
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
