"""仅计算输出命名，不执行文件写入。"""

import logging
import os
from pathlib import Path

from javsp.datatype import Movie
from javsp.scan.file import replace_illegal_chars

logger = logging.getLogger(__name__)


def _filesystem_limits(path: Path):
    parent = path.parent
    while parent != parent.parent:
        try:
            if parent.is_dir():
                break
        except OSError:
            # 规划中的名称可能已经超限；向上寻找可查询的现有目录。
            pass
        parent = parent.parent
    limits = []
    for name, default in (("PC_NAME_MAX", 255), ("PC_PATH_MAX", None)):
        try:
            value = os.pathconf(parent, name)
        except (AttributeError, OSError, ValueError):
            value = default
        limits.append(value if value is not None and value > 0 else default)
    return limits


def _path_violations(path, cfg, *, components_only=False):
    path = Path(os.path.abspath(path))
    name_max, path_max = _filesystem_limits(path)
    violations = []
    for part in path.parts[1:]:
        length = len(os.fsencode(part)) if os.name != "nt" else len(part.encode("utf-16-le")) // 2
        if length > name_max:
            violations.append((length - name_max, f"单个目录或文件名超过文件系统限制 {name_max}: {part}"))
    if not components_only:
        text = str(path)
        length = len(text.encode("utf-8")) if cfg.summarizer.path.length_by_byte else len(text)
        maximum = cfg.summarizer.path.length_maximum
        if length > maximum:
            violations.append((length - maximum, f"完整路径超过配置长度限制 {maximum}: {path}"))
        if path_max is not None and len(os.fsencode(text)) >= path_max:
            violations.append((len(os.fsencode(text)) - path_max + 1, f"完整路径超过文件系统限制 {path_max - 1}: {path}"))
    return violations


def validate_output_paths(paths, cfg):
    """写入前检查全部目标；配置字符计数不能绕过文件系统的名称限制。"""
    for path in paths:
        violations = _path_violations(path, cfg)
        if violations:
            raise ValueError(f"无法生成符合长度限制的输出路径，请缩短输出目录或命名模板；{violations[0][1]}")


def generate_names(movie: Movie, cfg, *, base_dir=None):
    """按模板生成路径，必要时仅缩短路径字段，保留完整元数据。"""
    base_dir = Path(base_dir or Path.cwd()).resolve()
    info = movie.info
    original = info.get_info_dic(cfg)
    actors = info.actress or []
    selected = actors[:cfg.summarizer.path.max_actress_count]
    original["actress"] = (
        ",".join(selected + (["…"] if len(selected) < len(actors) else [])) if actors else cfg.summarizer.default.actress
    )
    info.label = original["label"].upper()
    values = {key: replace_illegal_chars(value.strip()) for key, value in original.items()}
    info.nfo_title = cfg.summarizer.nfo.title_pattern.format(**values)
    original = values.copy()
    values = {key: value.replace("\n", "") for key, value in values.items()}
    values["num"] += movie.attr_str

    def render():
        if cfg.summarizer.move_files:
            save_dir = os.path.normpath(str(base_dir / cfg.summarizer.path.output_folder_pattern.format(**values))).strip()
            basename = cfg.summarizer.path.basename_pattern.format(**values).strip()
        else:
            save_dir = os.path.dirname(movie.files[0])
            basename = Path(movie.files[0]).stem
        if not basename or basename in (".", ".."):
            raise ValueError("命名模板生成了空的影片文件名")
        names = {
            "save_dir": save_dir,
            "basename": os.path.normpath(basename),
            "nfo_file": os.path.join(save_dir, cfg.summarizer.nfo.basename_pattern.format(**values) + ".nfo"),
            "fanart_file": os.path.join(save_dir, cfg.summarizer.fanart.basename_pattern.format(**values) + ".jpg"),
            "poster_file": os.path.join(save_dir, cfg.summarizer.cover.basename_pattern.format(**values) + ".jpg"),
        }
        paths = [names["nfo_file"], os.path.join(save_dir, "folder.jpg")]
        # 实际图片格式在下载后才确定，为 .webp/.tiff 等扩展名预留五个字符。
        paths.extend(os.path.splitext(names[key])[0] + ".tiff" for key in ("fanart_file", "poster_file"))
        if cfg.summarizer.move_files:
            for index, source in enumerate(movie.files, 1):
                suffix = f"-CD{index}" if len(movie.files) > 1 else ""
                extensions = [Path(source).suffix]
                if cfg.summarizer.match_subtitles:
                    extensions.extend((".srt", ".ass"))
                paths.extend(os.path.join(save_dir, names["basename"] + suffix + ext) for ext in extensions)
        if cfg.summarizer.extra_fanarts.enabled:
            paths.append(os.path.join(save_dir, "extrafanart", f"{max(1, len(info.preview_pics or []))}.tiff"))
        return names, paths

    def overflow(*, components_only=False):
        _, paths = render()
        return sum(amount for path in paths for amount, _ in _path_violations(path, cfg, components_only=components_only))

    def shorten(field, *, components_only=False):
        text = values[field]
        if len(text) <= 1:
            return
        before = overflow(components_only=components_only)

        def prefix(length):
            shortened = text[:length].rstrip()
            return shortened + ("…" if field == "actress" and length < len(text) else "")

        values[field] = prefix(1)
        minimum = overflow(components_only=components_only)
        if minimum >= before:
            values[field] = text
            return
        # 按 Unicode 字符边界找最长可用前缀；每次检查真实编码长度和全部模板。
        low, high = 1, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            values[field] = prefix(middle)
            if overflow(components_only=components_only) <= minimum:
                low = middle
            else:
                high = middle - 1
        values[field] = prefix(low)
        if field == "actress" and low < len(text) and "," in text[:low]:
            # 优先保留完整演员名；单个名字本身超长时才按字符截短。
            values[field] = text[:low].rsplit(",", 1)[0] + ",…"

    # 先解决独立超长的目录/文件名，避免为长演员目录白白删掉标题。
    for field in ("actress", "title", "rawtitle"):
        if overflow(components_only=True):
            shorten(field, components_only=True)
    for field in ("title", "rawtitle", "actress"):
        if overflow():
            shorten(field)
    names, paths = render()
    validate_output_paths(paths, cfg)
    for key, value in names.items():
        setattr(movie, key, value)
    for field, label in (("title", "标题"), ("rawtitle", "原始标题"), ("actress", "演员路径字段")):
        if values[field] != original[field]:
            logger.info("自动截短%s为:\n%s", label, values[field])
