"""获取和转换影片的各类番号（DVD ID, DMM cid, DMM pid）

番号识别采用规则引擎模式：每条识别规则独立注册，按优先级依次匹配。
新增番号格式只需添加一条规则，无需修改核心逻辑。

规则分两类：
- KeywordRule: 先匹配关键词，再提取番号（如 FC2、HEYDOUGA）
- PatternRule: 直接用正则匹配（如普通番号 ABC-123）

归一化（normalize）用于判断不同写法是否指向同一部影片：
  IPZ-380 / ipz380 / ipz00380 → IPZ-380
"""

import os
import re
from pathlib import Path

__all__ = ["get_id", "get_cid", "guess_av_type", "normalize_id"]

from javsp.config import Cfg


# =============================================================================
# 番号归一化：不同写法指向同一部影片时，生成统一的 key
# =============================================================================
def normalize_id(avid: str) -> str:
    """将番号归一化为统一格式，用于匹配判断

    示例:
        IPZ-380 / ipz380 / ipz00380 → IPZ-380
        FC2-123456 / fc2123456 → FC2-123456
    """
    if not avid:
        return ""
    d = avid.upper()
    # 带分隔符的标准格式: LETTERS-DIGITS
    m = re.match(r"^([A-Z]+)-(\d+)$", d)
    if m:
        return f"{m.group(1)}-{int(m.group(2))}"
    # 无分隔符格式: LETTERSDIGITS
    m = re.match(r"^([A-Z]+)(\d+)$", d)
    if m:
        return f"{m.group(1)}-{int(m.group(2))}"
    # FC2 等特殊格式已经带分隔符，直接返回
    return d


# =============================================================================
# 识别规则定义
# =============================================================================

class _KeywordRule:
    """关键词规则：先检查关键词，再用正则提取番号"""

    def __init__(self, keyword, pattern, formatter):
        self.keyword = keyword
        self.pattern = re.compile(pattern, re.I)
        self.formatter = formatter

    def match(self, norm):
        if self.keyword not in norm:
            return None
        m = self.pattern.search(norm)
        if m:
            return self.formatter(m)
        return None


class _PatternRule:
    """正则规则：直接用正则匹配"""

    def __init__(self, pattern, formatter):
        self.pattern = re.compile(pattern, re.I)
        self.formatter = formatter

    def match(self, norm):
        m = self.pattern.search(norm)
        if m:
            return self.formatter(m)
        return None


# =============================================================================
# 规则注册表（按优先级排列）
# =============================================================================

# --- 关键词规则（优先匹配，避免被普通规则误匹配） ---
_KEYWORD_RULES: list[_KeywordRule] = [
    _KeywordRule(
        "FC2",
        r"FC2[^A-Z\d]{0,5}(PPV[^A-Z\d]{0,5})?(\d{5,7})",
        lambda m: "FC2-" + m.group(2),
    ),
    _KeywordRule(
        "HEYDOUGA",
        r"(HEYDOUGA)[-_]*(\d{4})[-_]0?(\d{3,5})",
        lambda m: "-".join(m.groups()),
    ),
    _KeywordRule(
        "GETCHU",
        r"GETCHU[-_]*(\d+)",
        lambda m: "GETCHU-" + m.group(1),
    ),
    _KeywordRule(
        "GYUTTO",
        r"GYUTTO-(\d+)",
        lambda m: "GYUTTO-" + m.group(1),
    ),
    _KeywordRule(
        "259LUXU",
        r"259LUXU-(\d+)",
        lambda m: "259LUXU-" + m.group(1),
    ),
]

# --- 正则规则（按优先级排列，特殊格式在前，普通格式在后） ---
_PATTERN_RULES: list[_PatternRule] = [
    # 缩写成 hey 的 heydouga 影片（番号分三部分，先于两部分的匹配）
    _PatternRule(
        r"(?:HEY)[-_]*(\d{4})[-_]0?(\d{3,5})",
        lambda m: "heydouga-" + "-".join(m.groups()),
    ),
    # MUGEN 片商的奇怪番号（MK3D2DBD 模式，放在普通番号之前）
    _PatternRule(
        r"(MKB?D)[-_]*(S\d{2,3})|(MK3D2DBD|S2M|S2MBD)[-_]*(\d{2,3})",
        lambda m: (m.group(1) + "-" + m.group(2)) if m.group(1) else (m.group(3) + "-" + m.group(4)),
    ),
    # IBW 带后缀 z 的番号
    _PatternRule(
        r"(IBW)[-_](\d{2,5}z)",
        lambda m: m.group(1) + "-" + m.group(2),
    ),
    # 普通番号，带分隔符（如 ABC-123）
    _PatternRule(
        r"([A-Z]{2,10})[-_](\d{2,5})",
        lambda m: m.group(1) + "-" + m.group(2),
    ),
    # 东热 red, sky, ex 系列（不带分隔符，已停止更新，限制数字范围降低误匹配）
    _PatternRule(
        r"(RED[01]\d\d|SKY[0-3]\d\d|EX00[01]\d)",
        lambda m: m.group(1),
    ),
    # 普通番号，缺失分隔符（如 ABC123）
    _PatternRule(
        r"([A-Z]{2,})(\d{2,5})",
        lambda m: _format_no_sep(m),
    ),
    # TMA 影片（如 T28-557，番号很乱）
    _PatternRule(
        r"(T[23]8[-_]\d{3})",
        lambda m: m.group(1),
        # 注意：TMA 番号区分大小写，不使用 re.I
    ),
    # 东热 n, k 系列
    _PatternRule(
        r"(N\d{4}|K\d{4})",
        lambda m: m.group(1),
    ),
    # R18-XXX 番号
    _PatternRule(
        r"(R18-?\d{3})",
        lambda m: m.group(1),
    ),
    # 纯数字番号（无码影片）
    _PatternRule(
        r"(\d{6}[-_]\d{2,3})",
        lambda m: m.group(1),
        # 注意：纯数字番号区分大小写无意义，但也不需要 re.I
    ),
]


def _format_no_sep(m):
    """处理无分隔符的普通番号，去除多余前导零"""
    prefix = m.group(1)
    num = m.group(2)
    # 当数字部分超过3位且有前导零时，去除多余的前导零
    # （如 bbi00177 → BBI-177），但保留3位以内的前导零
    # （如 XVSR060 → XVSR-060，060 中的 0 是番号的一部分）
    if len(num) > 3 and num.startswith("0"):
        num = num.lstrip("0") or "0"
    return prefix + "-" + num


# =============================================================================
# 核心：从文件路径提取番号
# =============================================================================

def get_id(filepath_str: str) -> str:
    """从给定的文件路径中提取番号（DVD ID）"""
    filepath = Path(filepath_str)
    ignore_pattern = re.compile("|".join(Cfg().scanner.ignored_id_pattern))
    norm = ignore_pattern.sub("", filepath.stem).upper()

    return _extract_id(norm, filepath)


def _extract_id(norm: str, filepath: Path = None) -> str:
    """从规范化后的字符串中提取番号

    Args:
        norm: 已经过忽略模式过滤和大写化的文件名
        filepath: 原始路径（用于回退到文件夹名匹配）
    """
    # 1. 尝试关键词规则
    for rule in _KEYWORD_RULES:
        result = rule.match(norm)
        if result:
            return result

    # 2. 尝试移除可疑域名后重新匹配
    no_domain = re.sub(r"\w{3,10}\.(COM|NET|APP|XYZ)", "", norm, flags=re.I)
    if no_domain != norm:
        avid = _extract_id(no_domain)
        if avid:
            return avid

    # 3. 尝试正则规则
    for rule in _PATTERN_RULES:
        result = rule.match(norm)
        if result:
            return result

    # 4. 尝试将 ')(' 替换为 '-' 后再匹配
    if ")(" in norm:
        avid = _extract_id(norm.replace(")(", "-"))
        if avid:
            return avid

    # 5. 回退到文件所在文件夹名匹配
    if filepath and filepath.parent.name != "":
        return _extract_id(filepath.parent.name.upper())

    return ""


# =============================================================================
# CID 识别
# =============================================================================

CD_POSTFIX = re.compile(r"([-_]\w|cd\d)$")


def get_cid(filepath: str) -> str:
    """尝试将给定的文件名匹配为CID（Content ID）"""
    basename = os.path.splitext(os.path.basename(filepath))[0]
    # 移除末尾可能带有的分段影片序号
    possible = CD_POSTFIX.sub("", basename)
    # cid只由数字、小写字母和下划线组成
    match = re.match(r"^([a-z\d_]+)$", possible, re.A)
    if match:
        possible = match.group(1)
        if "_" not in possible:
            # 长度为7-14的cid就占了约99.01%. 最长的cid为24，但是长为20-24的比例不到十万分之五
            match = re.match(r"^[a-z\d]{7,19}$", possible)
            if match:
                return possible
        else:
            # 绝大多数都只有一个下划线（只有约万分之一带有两个下划线）
            match2 = re.match(
                r"""^h_\d{3,4}[a-z]{1,10}\d{2,5}[a-z\d]{0,8}$  # 约 99.17%
                                |^\d{3}_\d{4,5}$                            # 约 0.57%
                                |^402[a-z]{3,6}\d*_[a-z]{3,8}\d{5,6}$       # 约 0.09%
                                |^h_\d{3,4}wvr\d\w\d{4,5}[a-z\d]{0,8}$      # 约 0.06%
                                 $""",
                possible,
                re.VERBOSE,
            )
            if match2:
                return possible
    return ""


# =============================================================================
# 番号类型判断
# =============================================================================

def guess_av_type(avid: str) -> str:
    """识别给定的番号所属的分类: normal, fc2, cid, getchu, gyutto"""
    match = re.match(r"^FC2-\d{5,7}$", avid, re.I)
    if match:
        return "fc2"
    match = re.match(r"^GETCHU-(\d+)", avid, re.I)
    if match:
        return "getchu"
    match = re.match(r"^GYUTTO-(\d+)", avid, re.I)
    if match:
        return "gyutto"
    # 如果传入的avid完全匹配cid的模式，则将影片归类为cid
    cid = get_cid(avid)
    if cid == avid:
        return "cid"
    # 以上都不是: 默认归类为normal
    return "normal"


if __name__ == "__main__":
    print(get_id("FC2-123456/Unknown.mp4"))
