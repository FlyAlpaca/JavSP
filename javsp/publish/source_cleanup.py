"""批次结束后保护失败影片目录，核对转移与来源快照并事务清理残留。"""

import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from string import Formatter

from javsp.publish.planner import FileOperation
from javsp.publish.transaction import FileTransaction, RecoveryError, _stamp
from javsp.scan.avid import get_cid, get_id
from javsp.workflow.models import JobError, Stage

logger = logging.getLogger(__name__)


class _UnsafeCleanup(ValueError):
    """目录尚未满足删除条件，保留来源数据。"""


@dataclass
class _Tree:
    files: dict[Path, tuple]
    directories: dict[Path, tuple]


def _overlaps(left, right):
    return left.is_relative_to(right) or right.is_relative_to(left)


class SourceCleanup:
    def __init__(self, root, cfg, scan):
        self.root = Path(os.path.abspath(root))
        self.enabled = cfg.summarizer.move_files and not cfg.summarizer.path.hard_link
        self.cfg = cfg
        self.extensions = set(cfg.scanner.filename_extensions)
        patterns = cfg.scanner.ignored_folder_name_pattern
        self.ignored = re.compile("|".join(patterns)) if patterns else None
        # skip_nfo_dir 和过小文件属于扫描筛选，不是清理排除规则。
        self.protected = set()
        self.trees = {}
        self.directory_stamps = {}
        self.warnings = []
        self.source_files = {Path(os.path.abspath(source)) for movie in scan.movies for source in movie.files}
        self.output_prefix = self._output_prefix(cfg.summarizer.path.output_folder_pattern)
        if not self.enabled:
            return
        if self.output_prefix is None or self.output_prefix == self.root or self.root.is_relative_to(self.output_prefix):
            self.enabled = False
            self.warnings.append("无法确定独立的输出目录保护范围，跳过源目录清理")
            return
        self.protected.add(self.output_prefix)
        if cfg.summarizer.filemove_log:
            self.protected.add(self.root / "filemove.log")
        for handler in logging.getLogger("filemove").handlers:
            if isinstance(handler, logging.FileHandler):
                self.protected.add(Path(handler.baseFilename))
        for path in self.protected:
            if path != self.output_prefix and path.is_relative_to(self.root) and path.exists():
                self.warnings.append(f"保留正在使用的文件移动日志: {path}")
        try:
            self.root_stamp = _stamp(self.root)
            if self.root_stamp is None or not stat.S_ISDIR(self.root_stamp[2]):
                raise OSError("扫描根目录不是普通目录")
            self._snapshot(self.root)
        except OSError as error:
            self.enabled = False
            self.warnings.append(f"无法检查扫描根目录，跳过清理: {error}")

    def _output_prefix(self, pattern):
        # 只保护完整的固定目录组件，不将含字段的组件误当作固定前缀。
        fixed = []
        for component in Path(pattern).parts:
            if any(field is not None for _, field, _, _ in Formatter().parse(component)):
                break
            fixed.append(component.format())
        if not fixed:
            return None
        path = Path(os.path.abspath(self.root / Path(*fixed)))
        return path if path != Path(path.anchor) else None

    def _valid_video(self, path, stamp):
        return (path.suffix.lower() in self.extensions and stamp[3] >= self.cfg.scanner.minimum_size
                and bool(get_cid(str(path)) or get_id(str(path), self.cfg)))

    def _snapshot(self, directory):
        stamp = _stamp(directory)
        if stamp is None or not stat.S_ISDIR(stamp[2]):
            raise OSError(f"目录已变化: {directory}")
        self.directory_stamps[directory] = stamp[:3]
        with os.scandir(directory) as entries:
            children = [Path(entry.path) for entry in entries]
        if directory == self.root:
            self.root_children = set(children)
        for path in sorted(children):
            if any(path.is_relative_to(protected) for protected in self.protected):
                continue
            try:
                self.trees[path] = self._inventory(path)
            except _UnsafeCleanup as error:
                stamp = _stamp(path)
                if (stamp and stat.S_ISDIR(stamp[2]) and stamp[0] == self.root_stamp[0]
                        and not path.is_mount() and not path.name.startswith(".javsp-")
                        and not (self.ignored and self.ignored.match(path.name))):
                    self._snapshot(path)
                else:
                    self.protected.add(path)
                    self.warnings.append(f"保留 {path}: {error}")
                continue
            except OSError as error:
                self.protected.add(path)
                self.warnings.append(f"无法检查，保留 {path}: {error}")
                continue
            # 从已完成的清单派生所有子树，避免重复遍历文件系统。
            tree = self.trees[path]
            self.directory_stamps.update(tree.directories)
            for child in tree.directories:
                if child != path:
                    self.trees[child] = _Tree(
                        {file: stamp for file, stamp in tree.files.items() if file.is_relative_to(child)},
                        {folder: stamp for folder, stamp in tree.directories.items() if folder.is_relative_to(child)},
                    )
            for file, stamp in tree.files.items():
                self.trees.setdefault(file, _Tree({file: stamp}, {}))

    def _check_ancestors(self, path, directories=None):
        # 根目录的大小和时间会随文件移动而变化，只比较身份与类型。
        root_stamp = _stamp(self.root)
        if root_stamp is None or root_stamp[:3] != self.root_stamp[:3] or not stat.S_ISDIR(root_stamp[2]):
            raise _UnsafeCleanup("扫描根目录已变化或不是普通目录")
        if not path.is_relative_to(self.root):
            raise _UnsafeCleanup("目录不在扫描根目录以内")
        # 不沿祖先符号链接进入其他位置，也不跨挂载点。
        for parent in reversed((path, *path.parents)):
            if parent == self.root or not parent.is_relative_to(self.root):
                continue
            stamp = _stamp(parent)
            if stamp is None or not stat.S_ISDIR(stamp[2]):
                raise _UnsafeCleanup(f"目录已消失或不是普通目录: {parent}")
            if stamp[0] != root_stamp[0] or parent.is_mount():
                raise _UnsafeCleanup(f"目录位于其他挂载点: {parent}")
            if parent.name.startswith(".javsp-") or (self.ignored and self.ignored.match(parent.name)):
                raise _UnsafeCleanup(f"目录属于恢复材料或扫描排除目录: {parent}")
            expected = (directories or {}).get(parent, self.directory_stamps.get(parent))
            if expected is not None and stamp[:3] != expected:
                raise _UnsafeCleanup(f"源目录在清理前被替换: {parent}")
        return root_stamp

    def _inventory(self, path):
        if path == self.root:
            raise _UnsafeCleanup("保留扫描根目录")
        if path.name.startswith(".javsp-"):
            raise _UnsafeCleanup("保留恢复材料")
        stamp = _stamp(path)
        root_stamp = self._check_ancestors(path if stamp and stat.S_ISDIR(stamp[2]) else path.parent)
        if any(_overlaps(path, protected) for protected in self.protected):
            raise _UnsafeCleanup("目录包含被跳过的文件、输出或恢复材料")
        files, directories = {}, {}

        def visit(directory):
            stamp = _stamp(directory)
            if stamp is None or not stat.S_ISDIR(stamp[2]):
                raise _UnsafeCleanup(f"目录已变化: {directory}")
            if stamp[0] != root_stamp[0] or directory.is_mount():
                raise _UnsafeCleanup(f"目录位于其他挂载点: {directory}")
            if directory.name.startswith(".javsp-") or (self.ignored and self.ignored.match(directory.name)):
                raise _UnsafeCleanup(f"保留恢复材料或扫描排除目录: {directory}")
            directories[directory] = stamp[:3]
            with os.scandir(directory) as entries:
                for entry in entries:
                    child = Path(entry.path)
                    child_stamp = _stamp(child)
                    if child_stamp is None:
                        raise _UnsafeCleanup(f"目录内容已变化: {child}")
                    if stat.S_ISDIR(child_stamp[2]):
                        visit(child)
                    elif stat.S_ISREG(child_stamp[2]) and child_stamp[0] == root_stamp[0]:
                        if child.name == "recovery.json" or child.name.startswith(".javsp-"):
                            raise _UnsafeCleanup(f"保留恢复清单: {child}")
                        files[child] = child_stamp
                    else:
                        raise _UnsafeCleanup(f"保留符号链接或特殊文件: {child}")

        if stamp is None:
            return _Tree({}, {})
        if stat.S_ISREG(stamp[2]) and stamp[0] == root_stamp[0]:
            if path.name == "recovery.json":
                raise _UnsafeCleanup(f"保留恢复清单: {path}")
            return _Tree({path: stamp}, {})
        if not stat.S_ISDIR(stamp[2]):
            raise _UnsafeCleanup(f"保留符号链接或特殊文件: {path}")
        visit(path)
        return _Tree(files, directories)

    def _eligible(self, path, original, moved):
        targets = {}
        for source, source_stamp in original.files.items():
            if source in self.source_files and source not in moved:
                raise _UnsafeCleanup(f"本轮影片缺少成功转移记录: {source}")
            if source not in moved:
                continue
            target = moved[source]
            target_stamp = _stamp(target)
            if (os.path.lexists(source) or target_stamp is None or not stat.S_ISREG(target_stamp[2])
                    or target_stamp[3] != source_stamp[3]):
                raise _UnsafeCleanup(f"无法确认文件转移结果: {source} -> {target}")
            targets[target] = target_stamp
        current = self._inventory(path)
        expected = {file: stamp for file, stamp in original.files.items() if file not in moved}
        if current.directories != original.directories or current.files != expected:
            raise _UnsafeCleanup("源目录内容已变化或仍有未转移文件")
        return current, targets

    def _remove(self, path, tree, targets):
        tx = FileTransaction()
        try:
            for source, stamp in sorted(tree.files.items()):
                # 每次移动前核对祖先身份，避免目录被替换后沿链接操作。
                self._check_ancestors(source.parent, tree.directories)
                target = tx.temp(self.root / "source-cleanup")
                os.unlink(target)
                tx.move(FileOperation(str(source), target, stamp))
            self._check_root_changes(tx.workspaces.values())
            remaining = self._inventory(path)
            if remaining.files or remaining.directories != tree.directories:
                raise _UnsafeCleanup("暂存后源目录出现新增内容")
            for target, stamp in targets.items():
                tx.check(target, stamp)
            for entry in tx.entries:
                tx.check(entry.target, entry.stamp)
        except BaseException as cause:
            errors = tx.rollback()
            if errors:
                paths = tx.preserve(cause, errors)
                raise RecoveryError(cause, errors, paths) from cause
            self.warnings.extend(tx.cleanup())
            raise

        # 暂存全部成功才进入删除阶段；此后不回滚已授权删除的残留文件。
        try:
            warnings = tx.cleanup()
        except BaseException as cause:
            detail = "残留文件清理被中断；部分暂存文件可能已经删除"
            paths = tx.preserve(cause, [detail])
            self.warnings.append(f"{detail}，请保留并核对工作区: {', '.join(paths)}")
            raise
        self.warnings.extend(warnings)
        if warnings:
            tx.preserve(OSError("暂存目录清理失败"), warnings)
            return
        for directory in sorted(tree.directories, key=lambda item: len(item.parts), reverse=True):
            try:
                self._check_ancestors(directory, tree.directories)
                os.rmdir(directory)
            except (OSError, _UnsafeCleanup) as error:
                self.warnings.append(f"删除空源目录失败，保留 {directory}: {error}")
        if not path.exists():
            logger.info("已清理源目录残留: %s", path)

    def _prepare(self, batch):
        self._check_ancestors(self.root)
        moved = {}
        for job in batch.jobs:
            if job.success:
                moved.update((Path(os.path.abspath(source)), Path(os.path.abspath(target)))
                             for source, target in job.output.moved_files)
                self.protected.update(Path(os.path.abspath(file)) for file in job.output.skipped_files)
                self.protected.add(Path(os.path.abspath(job.output.movie.save_dir)))
                self.protected.update(Path(os.path.abspath(file)) for file in job.output.artifacts)
                self.protected.update(Path(os.path.abspath(target)) for _, target in job.output.moved_files)
        for source in self.source_files - moved.keys():
            self.protected.add(source.parent)
            self.warnings.append(f"保留失败或缺少成功转移记录的影片目录: {source.parent}")
        self._check_root_changes()

        files = {file: stamp for tree in self.trees.values() for file, stamp in tree.files.items()}
        for file, stamp in files.items():
            if file not in moved and self._valid_video(file, stamp):
                if file.parent not in self.protected:
                    self.protected.add(file.parent)
                    self.warnings.append(f"保留有效未处理影片及配套目录: {file.parent}")
        return moved

    def _check_root_changes(self, workspaces=()):
        self._check_ancestors(self.root)
        workspaces = {Path(path) for path in workspaces}
        with os.scandir(self.root) as entries:
            for entry in entries:
                path = Path(entry.path)
                if (path not in self.root_children and path not in workspaces
                        and not any(_overlaps(path, protected) for protected in self.protected)):
                    raise _UnsafeCleanup(f"扫描根目录出现新增内容，跳过清理: {path}")

    def run(self, batch):
        if (not self.enabled or batch.aborted or batch.error or batch.pending or len(batch.jobs) != batch.total
                or any(job.cancelled or job.interruption or (job.error and not job.error.recovered) for job in batch.jobs)):
            batch.warnings.extend(self.warnings)
            for warning in self.warnings:
                logger.warning(warning)
            return

        # 先核对所有候选，选取互不重叠的最上层合格目录，避免父子清理互相改变快照。
        selected = []
        try:
            moved = self._prepare(batch)
            for path, original in sorted(self.trees.items(), key=lambda item: len(item[0].parts)):
                if any(path.is_relative_to(parent) for parent in selected):
                    continue
                if any(path.is_relative_to(protected) for protected in self.protected):
                    continue
                try:
                    self._eligible(path, original, moved)
                    selected.append(path)
                except _UnsafeCleanup as error:
                    self.warnings.append(f"保留 {path}: {error}")
                    if not any(_overlaps(path, protected) for protected in self.protected):
                        self.protected.add(path if original.directories else path.parent)
                except OSError as error:
                    self.warnings.append(f"无法复核源目录，保留 {path}: {error}")
                    self.protected.add(path if original.directories else path.parent)
            for path in selected:
                try:
                    # 清理其他目录或外部操作可能改变状态，暂存前再次核对。
                    tree, targets = self._eligible(path, self.trees[path], moved)
                    self._remove(path, tree, targets)
                except (OSError, _UnsafeCleanup) as error:
                    self.warnings.append(f"清理源目录失败，保留 {path}: {error}")
                    # 其他路径可能也已变化，停止本轮剩余清理。
                    break
        except (OSError, _UnsafeCleanup) as error:
            self.warnings.append(f"跳过源目录清理: {error}")
        except RecoveryError as error:
            batch.aborted = True
            batch.error = JobError("", Stage.CLEANUP, error, False, error.paths)
            logger.error("源目录清理恢复不完整，停止清理: %s", error)
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as error:
            batch.aborted = True
            batch.error = JobError("", Stage.CLEANUP, error)
        finally:
            batch.warnings.extend(self.warnings)
            for warning in self.warnings:
                logger.warning(warning)
