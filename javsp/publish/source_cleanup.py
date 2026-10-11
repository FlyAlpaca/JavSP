"""批次结束后核对影片转移结果，事务暂存残留文件，再删除源子目录。"""

import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from javsp.publish.planner import FileOperation
from javsp.publish.transaction import FileTransaction, RecoveryError, _stamp
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
        self.extensions = set(cfg.scanner.filename_extensions)
        patterns = cfg.scanner.ignored_folder_name_pattern
        self.ignored = re.compile("|".join(patterns)) if patterns else None
        self.protected = {Path(os.path.abspath(path)) for path in scan.skipped}
        self.trees = {}
        self.warnings = []
        if not self.enabled:
            return
        try:
            self.root_stamp = _stamp(self.root)
        except OSError as error:
            self.warnings.append(f"无法检查扫描根目录，保留源子目录: {error}")
            return
        if self.root_stamp is None or not stat.S_ISDIR(self.root_stamp[2]):
            return
        candidates = set()
        for movie in scan.movies:
            for source in movie.files:
                path = Path(os.path.abspath(source)).parent
                while path != self.root and path.is_relative_to(self.root):
                    candidates.add(path)
                    path = path.parent
        for path in sorted(candidates):
            try:
                self.trees[path] = self._inventory(path)
            except _UnsafeCleanup as error:
                logger.debug("保留源目录 %s: %s", path, error)
            except OSError as error:
                self.warnings.append(f"无法检查源目录，保留 {path}: {error}")

    def _check_ancestors(self, path, directories=None):
        # 根目录的大小和时间会随文件移动而变化，只比较身份与类型。
        root_stamp = _stamp(self.root)
        if root_stamp is None or root_stamp[:3] != self.root_stamp[:3] or not stat.S_ISDIR(root_stamp[2]):
            raise _UnsafeCleanup("扫描根目录已变化或不是普通目录")
        if path == self.root or not path.is_relative_to(self.root):
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
            if directories is not None and parent in directories and stamp[:3] != directories[parent]:
                raise _UnsafeCleanup(f"源目录在清理前被替换: {parent}")
        return root_stamp

    def _inventory(self, path):
        root_stamp = self._check_ancestors(path)
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
                        if child.name == "recovery.json":
                            raise _UnsafeCleanup(f"保留恢复清单: {child}")
                        files[child] = child_stamp
                    else:
                        raise _UnsafeCleanup(f"保留符号链接或特殊文件: {child}")

        visit(path)
        return _Tree(files, directories)

    def _eligible(self, path, original, moved):
        videos = {file: stamp for file, stamp in original.files.items() if file.suffix.lower() in self.extensions}
        if not videos or not videos.keys() <= moved.keys():
            raise _UnsafeCleanup("原有影片未全部成功转移")
        targets = {}
        for source, source_stamp in videos.items():
            target = moved[source]
            target_stamp = _stamp(target)
            if (os.path.lexists(source) or target_stamp is None or not stat.S_ISREG(target_stamp[2])
                    or target_stamp[3] != source_stamp[3]):
                raise _UnsafeCleanup(f"无法确认影片转移结果: {source} -> {target}")
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
            logger.info("影片全部转移，已删除源子目录及残留文件: %s", path)

    def run(self, batch):
        if not self.enabled or batch.aborted:
            return
        moved = {}
        for job in batch.jobs:
            if job.success:
                moved.update((Path(source), Path(target)) for source, target in job.output.moved_files)
                self.protected.update(Path(file) for file in job.output.skipped_files)
                self.protected.add(Path(job.output.movie.save_dir))
                self.protected.update(Path(file) for file in job.output.artifacts)
                self.protected.update(Path(target) for _, target in job.output.moved_files)

        # 先核对所有候选，选取互不重叠的最上层合格目录，避免父子清理互相改变快照。
        selected = []
        try:
            for path, original in sorted(self.trees.items(), key=lambda item: len(item[0].parts)):
                if any(path.is_relative_to(parent) for parent in selected):
                    continue
                try:
                    self._eligible(path, original, moved)
                    selected.append(path)
                except _UnsafeCleanup as error:
                    logger.debug("保留源目录 %s: %s", path, error)
                except OSError as error:
                    self.warnings.append(f"无法复核源目录，保留 {path}: {error}")
            for path in selected:
                try:
                    # 清理其他目录或外部操作可能改变状态，暂存前再次核对。
                    tree, targets = self._eligible(path, self.trees[path], moved)
                    self._remove(path, tree, targets)
                except (OSError, _UnsafeCleanup) as error:
                    self.warnings.append(f"清理源目录失败，保留 {path}: {error}")
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
