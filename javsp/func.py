"""业务逻辑所需的或有一定通用性的函数"""

# 为了降低耦合度，也避免功能复杂后可能出现的循环导入的问题，这里尽量不导入项目内部的模块
# 如果需要获得配置信息，也应当由外部模块将配置项的值以参数的形式传入
import logging
import re
from pathlib import Path

from javsp.lib import re_escape

__all__ = [
    "get_scan_dir",
    "remove_trail_actor_in_title",
    "split_by_punc",
]


logger = logging.getLogger(__name__)


def get_scan_dir(cfg_scan_dir: Path | None) -> str | None:
    """综合命令参数、配置文件等信息，返回要扫描影片的文件夹"""
    # 目前config模块负责处理来自命令行和来自文件的配置，cfg_scan_dir已经是综合了这两处后得到的结果
    if cfg_scan_dir and cfg_scan_dir.is_dir():
        return str(cfg_scan_dir)

    if cfg_scan_dir:
        logger.warning(f"配置的待整理文件夹不存在或不是目录：'{cfg_scan_dir}'")

    return None


def remove_trail_actor_in_title(title: str, actors: list) -> str:
    """寻找并移除标题尾部的女优名"""
    if not (actors and title):
        return title
    # 目前使用分隔符白名单来做检测（担心按Unicode范围匹配误伤太多），考虑尽可能多的分隔符
    delimiters = "-xX &·,;　＆・，；"
    actor_ls = [re_escape(i) for i in actors if i]
    pattern = f"^(.*?)([{delimiters}]{{1,3}}({'|'.join(actor_ls)}))+$"
    # 使用match而不是sub是为了将替换掉的部分写入日志
    match = re.match(pattern, title)
    if match:
        logger.debug(f"移除标题尾部的女优名: '{match.group(1)}' [{match.group(2)}]")
        return match.group(1)
    else:
        return title


# 枚举Unicode各平面内中日韩区块及拉丁字母区块内的所有标点符号
_punc = (
    " 　",  # spaces
    "!\"#%&'()*,-./:;?@[\\]_{}",  # (0x0, 0x7f), Basic Latin
    "¡§«¶·»¿",  # (0x80, 0xff), Latin-1 Supplement
    ";·",  # (0x370, 0x3ff), Greek and Coptic
    "‐‑‒–—―‖‗‘’‚‛“”„‟†‡•‣․‥…‧‰‱′″‴‵‶‷‸‹›※‼‽‾‿⁀⁁⁂⁃⁅⁆⁇⁈⁉⁊⁋⁌⁍⁎⁏⁐⁑⁓⁔⁕⁖⁗⁘⁙⁚⁛⁜⁝⁞",  # (0x2000, 0x206f), General Punctuation
    "、。〃〈〉《》「」『』【】〔〕〖〗〘〙〚〛〜〝〞〟〰〽",  # (0x3000, 0x303f), CJK Symbols and Punctuation
    "゠・",  # (0x30a0, 0x30ff), Katakana
    "︐︑︒︓︔︕︖︗︘︙",  # (0xfe10, 0xfe1f), Vertical Forms
    "︰︱︲︳︴︵︶︷︸︹︺︻︼︽︾︿﹀﹁﹂﹃﹄﹅﹆﹇﹈﹉﹊﹋﹌﹍﹎﹏",  # (0xfe30, 0xfe4f), CJK Compatibility Forms
    # "﹐﹑﹒﹔﹕﹖﹗﹘﹙﹚﹛﹜﹝﹞﹟﹠﹡﹣﹨﹪﹫",                                  # (0xfe50, 0xfe6f), Small Form Variants
    "！＂＃％＆＇（）＊，－．／：；？＠［＼］＿｛｝｟｠｡｢｣､･",  # (0xff00, 0xffef), Halfwidth and Fullwidth Forms
)
_punc_pattern = re.compile(".*?[" + "".join(_punc) + "]")


def split_by_punc(s):
    """将一个字符串按照Unicode标准中的标点符号进行分割"""
    iters = list(_punc_pattern.finditer(s))
    if iters:
        ls = [s[i.span()[0] : i.span()[1]] for i in iters]
        ls.append(s[iters[-1].span()[1] :])
    else:
        ls = [s]
    return ls
