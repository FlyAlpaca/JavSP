"""只读输出规划、目标冲突校验和文件操作描述。"""

import os
import re
from copy import copy
from dataclasses import dataclass
from pathlib import Path

from javsp.datatype import Movie, MovieInfo
from javsp.publish.naming import generate_names, validate_output_paths
from javsp.publish.transaction import _key, _stamp
from javsp.scan.avid import get_id, normalize_id


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
    def __init__(self, cfg, *, root=None):
        self.cfg = cfg
        self.root = Path(root or Path.cwd()).resolve()

    def plan(self, movie: Movie, info: MovieInfo) -> OutputPlan:
        planned = _snapshot(movie, info)
        generate_names(planned, self.cfg, base_dir=self.root)
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
        validate_output_paths([op.target for op in operations if not op.skip] + artifacts, self.cfg)
        _validate_targets(operations, artifacts, planned.files)
        for path in artifacts:
            if os.path.lexists(path) and (not os.path.isfile(path) or os.path.islink(path)):
                raise ValueError(f"产物目标不是普通文件: {path}")
        return OutputPlan(planned, operations, tuple(artifacts), warnings)
