"""GitHub 演员映射缓存、别名匹配与统一展示名称。"""

import hashlib
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from importlib.resources import files
from pathlib import Path

import requests
from lxml import etree
from opencc import OpenCC

from javsp.download import DownloadError, DownloadSpec, ObjectChanged, clear_parts, download_file

logger = logging.getLogger(__name__)
MAPPING_URL = "https://raw.githubusercontent.com/li-peifeng/Jav-Actors-Mapping/main/actor-mapping.xml"
MAX_MAPPING_SIZE = 32 * 1024 * 1024
DOWNLOAD_DEADLINE = 600
MAPPING_PART_SIZE = 1024 * 1024
DOWNLOAD_ATTEMPTS = 3
_KANA = re.compile(r"[\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fc-\u30ff\u31f0-\u31ff\uff66-\uff9f]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")
_COMPOSITE = re.compile(r"([^()（）]+)[(（]([^()（）]+)[)）]")


def _chinese_name(*names: str) -> str:
    """中文字段可能只是日文或罗马音占位；仅从有效字段中按顺序取值。"""
    return next((name for name in names if _HAN.search(name) and not _KANA.search(name) and "々" not in name), "")


class _NameNormalizer:
    """通用日文异体字、重复符号及简繁归一；不包含演员姓名。"""

    def __init__(self):
        self._converter = OpenCC("t2s")
        variants = {}
        data = files("javsp.metadata").joinpath("data/kanji-variants.tsv").read_text(encoding="utf-8")
        for line in data.splitlines():
            if not line or line.startswith("#"):
                continue
            key, values = line.split("\t")
            candidates = {self._converter.convert(value) for value in values.split()}
            if len(candidates) == 1:
                variants[ord(key)] = candidates.pop()
        self._variants = variants
        self._cache: dict[str, str] = {}

    def __call__(self, name):
        if name not in self._cache:
            expanded = []
            for char in unicodedata.normalize("NFKC", name).strip():
                if char == "々" and expanded and _HAN.fullmatch(expanded[-1]):
                    char = expanded[-1]
                expanded.append(char)
            chinese = self._converter.convert("".join(expanded))
            self._cache[name] = self._converter.convert(chinese.translate(self._variants))
        return self._cache[name]


def _mapping_root(content):
    if len(content) > MAX_MAPPING_SIZE:
        raise ActorMappingError("演员映射文件过大")
    try:
        parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
        root = etree.fromstring(content, parser)
    except etree.XMLSyntaxError as error:
        raise ActorMappingError(f"演员映射 XML 无效: {error}") from error
    if root.tag != "actor-mapping" or root.getroottree().docinfo.doctype:
        raise ActorMappingError("演员映射根节点无效或含有 DTD")
    return root


class ActorMappingError(Exception):
    """没有可用的演员映射，或更新数据无效。"""


class ActorMapping:
    """一次构建、整批复用的名称索引；冲突名称不会选取任意条目。"""

    def __init__(self):
        self._japanese: dict[str, set[tuple[str, str]]] = {}
        self._primary: dict[str, set[tuple[str, str]]] = {}
        self._aliases: dict[str, set[tuple[str, str]]] = {}
        self._fallback_chinese: dict[str, str] = {}
        self._named_chinese: dict[tuple[str, str], set[str]] = {}
        self._normalized_names: dict[str, set[str]] = {}
        self._normalize = _NameNormalizer()
        self.row_count = 0

    @classmethod
    def from_xml(cls, content: bytes, *, supplement: bytes | None = None):
        root = _mapping_root(content)
        mapping = cls()
        for row in root.findall(".//a"):
            if mapping._add_row(row):
                mapping.row_count += 1
        if not mapping.row_count:
            raise ActorMappingError("演员映射没有包含原名的有效条目")
        if supplement is not None:
            for row in _mapping_root(supplement).findall(".//a"):
                mapping._add_row(row, supplemental=True)
        for index in (mapping._japanese, mapping._primary, mapping._aliases):
            for name, actors in index.items():
                mapping._normalized_names.setdefault(mapping._normalize(name), set()).update(jp for jp, _ in actors)
        mapping._build_chinese_fallbacks()
        return mapping

    def _add_row(self, row, *, supplemental=False):
        jp = (row.get("jp") or "").strip()
        if not jp:
            return False
        cn, tw = ((row.get(key) or "").strip() for key in ("zh_cn", "zh_tw"))
        chinese = _chinese_name(*(name for name in (cn, tw) if name != jp)) or _chinese_name(cn, tw)
        existing = self._japanese.get(jp, set())
        add_main = True
        if supplemental:
            if not row.get("source"):
                raise ActorMappingError("补充演员映射缺少来源")
            # 上游身份及有效译名优先；无主名译名的补充行只能扩展已有演员的别名。
            if not existing and (not chinese or self._lookup(jp)[0]):
                return False
            add_main = not any(value and value != jp for _, value in existing)
            if not chinese and existing:
                add_main = False
        actor = (jp, chinese)
        if add_main:
            self._japanese.setdefault(jp, set()).add(actor)
            display = f"{jp}（{chinese}）" if chinese and chinese != jp else jp
            for name in (cn, tw, display):
                if name:
                    self._primary.setdefault(name, set()).add(actor)
            for name in (row.get("keyword") or "").split(","):
                if name.strip():
                    self._aliases.setdefault(name.strip(), set()).add(actor)
        for alias in row.findall("alias"):
            name = (alias.get("jp") or "").strip()
            value = _chinese_name(*((alias.get(key) or "").strip() for key in ("zh_cn", "zh_tw")))
            if not name or not value:
                raise ActorMappingError("演员别名专属译名无效")
            pair = (jp, name)
            if supplemental and pair in self._named_chinese:
                continue
            self._named_chinese.setdefault(pair, set()).add(value)
            self._aliases.setdefault(name, set()).add(actor)
            self._primary.setdefault(value, set()).add(actor)
        return True

    def _build_chinese_fallbacks(self):
        """按汉字片段匹配上游别名；简繁等价候选合并，跨演员归属冲突不补全。"""
        normalized: dict[str, str] = {}
        for index in (self._japanese, self._primary, self._aliases):
            for name, actors in index.items():
                value = self._normalize(name)
                if all(_HAN.fullmatch(char) for char in value):
                    normalized[name] = value

        keywords: dict[str, set[str]] = {}
        for name, actors in self._aliases.items():
            if name in normalized:
                for jp, _ in actors:
                    keywords.setdefault(jp, set()).add(normalized[name])

        for jp, actors in self._japanese.items():
            # 纯汉字原名须有同名中文字段或简繁等价别名作为依据。
            if jp in normalized:
                chinese = normalized[jp]
                if (jp, jp) not in actors and chinese not in keywords.get(jp, ()):
                    continue
            else:
                name = self._normalize(jp)
                # 至少保留一个汉字锚点；候选仍须唯一且无跨演员归属冲突。
                if not _HAN.search(name) or not _KANA.search(name):
                    continue
                if not all(_HAN.fullmatch(char) or _KANA.fullmatch(char) for char in name):
                    continue
                pattern = re.sub(f"{_KANA.pattern}+", ".+", name)
                candidates = {alias for alias in keywords.get(jp, ()) if re.fullmatch(pattern, alias)}
                if len(candidates) != 1:
                    continue
                chinese = candidates.pop()
            if self._normalized_names[chinese] == {jp}:
                self._fallback_chinese[jp] = chinese

    @classmethod
    def from_file(cls, path: Path):
        try:
            if path.stat().st_size > MAX_MAPPING_SIZE:
                raise ActorMappingError("演员映射文件过大")
            return cls.from_xml(path.read_bytes())
        except OSError as error:
            raise ActorMappingError(f"无法读取演员映射缓存 {path}: {error}") from error

    def _lookup(self, key: str):
        for index in (self._japanese, self._primary, self._aliases):
            candidates = index.get(key)
            if candidates:
                return {jp for jp, _ in candidates}, index is self._aliases
        originals = self._normalized_names.get(self._normalize(key), set())
        is_alias = len(originals) == 1 and self._normalize(key) != self._normalize(next(iter(originals)))
        return originals, is_alias

    def _translation(self, jp: str, name: str | None = None) -> str | None:
        if name is not None and (translations := self._named_chinese.get((jp, name))):
            if len(translations) != 1 or self._normalized_names.get(self._normalize(name)) != {jp}:
                return None
            # 显式对应关系由父条目确定身份；不同演员允许拥有相同中文名。
            return next(iter(translations))
        # 同一原名的占位记录不应遮住有效译名；冲突译名保持未决。
        translations = {cn for _, cn in self._japanese[jp] if cn and cn != jp}
        if len(translations) > 1:
            return None
        chinese = next(iter(translations), jp if (jp, jp) in self._japanese[jp] else "")
        if not chinese or chinese == jp:
            chinese = self._fallback_chinese.get(jp, chinese)
        return chinese

    def resolve(self, name: str, *, normalize_aliases=True) -> str:
        key = name.strip()
        originals, is_alias = self._lookup(key)
        composite = None
        if not originals and (match := _COMPOSITE.fullmatch(key)):
            first, second = (part.strip() for part in match.groups())
            originals, _ = self._lookup(first)
            if len(originals) != 1 or self._lookup(second)[0] != originals:
                return name
            composite = first
        if len(originals) != 1:
            return name
        jp = next(iter(originals))
        original = composite or (key if is_alias and not normalize_aliases else jp)
        chinese = self._translation(jp, original)
        if chinese is None:
            return name
        return f"{original}（{chinese}）" if chinese and chinese != original else original


def _atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".actor-mapping-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _mapping_content(path, session, headers, options):
    """先检查缓存版本，再探测下载；强 ETag 与准确范围响应才允许复用分片。"""
    cached_etag = headers.get("If-None-Match")
    if cached_etag:
        with session.head(
            MAPPING_URL, headers={**headers, "Accept-Encoding": "identity"},
            allow_redirects=True, timeout=(30, 120), **options
        ) as response:
            if response.status_code == 304:
                return None, None
            # 部分服务器忽略条件请求；有效缓存与远端 ETag 相同仍可直接复用。
            if response.status_code == 200 and response.headers.get("ETag") == cached_etag:
                return None, None
            if response.status_code not in (405, 501):
                response.raise_for_status()
    request_headers = {**headers, "Accept-Encoding": "identity", "Range": "bytes=0-0"}
    with session.get(MAPPING_URL, headers=request_headers, stream=True, timeout=(30, 120), **options) as response:
        if response.status_code == 304:
            return None, None
        response.raise_for_status()
        new_etag = response.headers.get("ETag")
        if response.status_code in (200, 206) and cached_etag and new_etag == cached_etag:
            return None, None
        match = re.fullmatch(r"bytes 0-0/(\d+)", response.headers.get("Content-Range", ""))
        if response.status_code == 206 and match and new_etag and not new_etag.startswith("W/"):
            size = int(match.group(1))
            if not 0 < size <= MAX_MAPPING_SIZE:
                raise ActorMappingError("演员映射文件大小无效或过大")
        else:
            size = None
            if response.status_code == 200:
                return _read_mapping_response(response), new_etag
    if size is None:
        # 不支持可靠范围/对象身份时重新请求完整响应，不能将探测字节当作 XML。
        with session.get(
            MAPPING_URL, headers={**headers, "Accept-Encoding": "identity"}, stream=True, timeout=(30, 120), **options
        ) as response:
            response.raise_for_status()
            return _read_mapping_response(response), response.headers.get("ETag")
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(f"{MAPPING_URL}\0{size}\0{new_etag}\0{MAPPING_PART_SIZE}".encode()).hexdigest()[:24]
    prefix = path.parent / f".{path.name}-download-{identity}"
    # 只清理本缓存的旧版本分片，不触及其他运行资源。
    for stale in path.parent.glob(f".{path.name}-download-*"):
        if not stale.name.startswith(prefix.name + ".") and stale.is_file():
            stale.unlink()
    spec = DownloadSpec(MAPPING_URL, size, prefix, new_etag, part_size=MAPPING_PART_SIZE)
    assembled = None
    try:
        assembled = download_file(spec, client=session, options=options, label="下载 GitHub 演员映射", logger=logger)
        content = assembled.read_bytes()
        ActorMapping.from_xml(content)
    except (ObjectChanged, ActorMappingError):
        clear_parts(spec)
        raise
    else:
        clear_parts(spec)
        return content, new_etag
    finally:
        if assembled is not None:
            assembled.unlink(missing_ok=True)


def _read_mapping_response(response):
    if response.status_code != 200:
        raise ActorMappingError(f"演员映射响应状态异常: {response.status_code}")
    if int(response.headers.get("Content-Length", "0")) > MAX_MAPPING_SIZE:
        raise ActorMappingError("演员映射文件过大")
    logger.info("下载 GitHub 演员映射（单连接）")
    started = last_report = time.monotonic()
    content = bytearray()
    for chunk in response.iter_content(64 * 1024):
        now = time.monotonic()
        if now - started > DOWNLOAD_DEADLINE:
            raise ActorMappingError("演员映射下载超过 10 分钟")
        content.extend(chunk)
        if len(content) > MAX_MAPPING_SIZE:
            raise ActorMappingError("演员映射文件过大")
        if now - last_report >= 30:
            logger.info("演员映射已下载 %.1f MiB", len(content) / (1024 * 1024))
            last_report = now
    return bytes(content)


def prepare_actor_mapping(path: Path, *, session=requests, proxy=None) -> ActorMapping:
    """每次调用检查更新；只有完整、可解析的新表才能替换缓存。"""
    try:
        supplement = files("javsp.metadata").joinpath("data/actor-supplement.xml").read_bytes()
    except OSError as error:
        raise ActorMappingError(f"无法读取随包演员补充数据: {error}") from error
    cached = None
    cached_content = b""
    metadata_path = path.with_suffix(".json")
    etag = None
    try:
        if path.stat().st_size > MAX_MAPPING_SIZE:
            raise ActorMappingError("演员映射文件过大")
        cached_content = path.read_bytes()
        cached = ActorMapping.from_xml(cached_content, supplement=supplement)
    except (OSError, ActorMappingError) as error:
        logger.info("演员映射缓存不可用，将从 GitHub 获取: %s", error)
    if cached is not None:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("source") == MAPPING_URL and metadata.get("sha256") == hashlib.sha256(cached_content).hexdigest():
                value = metadata.get("etag")
                etag = value if isinstance(value, str) and value else None
        except (OSError, ValueError, AttributeError):
            pass

    logger.info("检查 GitHub 演员映射更新")
    headers = {"Cache-Control": "no-cache"}
    if etag:
        headers["If-None-Match"] = etag
    options = {"proxies": {"http": str(proxy), "https": str(proxy)}} if proxy is not None else {}
    try:
        for attempt in range(DOWNLOAD_ATTEMPTS):
            try:
                content, new_etag = _mapping_content(path, session, headers, options)
                break
            except (requests.RequestException, DownloadError) as error:
                if attempt + 1 == DOWNLOAD_ATTEMPTS:
                    raise
                logger.warning("演员映射检查或下载暂时失败，将重试（%s/%s）: %s", attempt + 1, DOWNLOAD_ATTEMPTS, error)
        if content is None:
            if cached is not None and etag:
                logger.debug("演员映射没有更新，复用缓存（%s 条）", cached.row_count)
                return cached
            raise ActorMappingError("演员映射返回 304 但没有对应有效缓存")
        updated = ActorMapping.from_xml(content, supplement=supplement)
        if cached is None or content != cached_content:
            _atomic_write(path, content)
    except (requests.RequestException, DownloadError, ActorMappingError, OSError, ValueError) as error:
        if cached is not None:
            logger.warning("演员映射更新失败，继续使用有效缓存: %s", error)
            return cached
        raise ActorMappingError(f"GitHub 演员映射获取失败且无有效缓存: {error}") from error

    # XML 先发布；中断或元数据写入失败只会让下次启动重新下载，不会错误复用 ETag。
    metadata = {"source": MAPPING_URL, "etag": new_etag, "sha256": hashlib.sha256(content).hexdigest()}
    try:
        _atomic_write(metadata_path, json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
    except OSError as error:
        logger.warning("演员映射已更新，版本元数据保存失败，下次将重新检查完整文件: %s", error)
    logger.info("GitHub 演员映射已就绪（%s 条）: %s", updated.row_count, path)
    return updated
