"""GitHub 演员映射缓存、别名匹配与统一展示名称。"""

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

import requests
from lxml import etree

from javsp.download import DownloadError, DownloadSpec, ObjectChanged, clear_parts, download_file

logger = logging.getLogger(__name__)
MAPPING_URL = "https://raw.githubusercontent.com/li-peifeng/Jav-Actors-Mapping/main/actor-mapping.xml"
MAX_MAPPING_SIZE = 32 * 1024 * 1024
DOWNLOAD_DEADLINE = 600
MAPPING_PART_SIZE = 1024 * 1024
DOWNLOAD_ATTEMPTS = 3
_KANA = re.compile(r"[\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fc-\u30ff\u31f0-\u31ff\uff66-\uff9f]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")
# 上游中文字段为日文占位，但 keyword 已收录的中文译名。
# 只允许经核实的条目，且运行时仍须确认别名唯一属于该原名，不能任取汉字别名（可能是旧艺名）。
_VERIFIED_CHINESE_ALIASES = {"雛形みくる": ("雏形美久琉", "雛形美久琉")}


def _chinese_name(*names: str) -> str:
    """中文字段可能只是日文或罗马音占位；仅从有效字段中按顺序取值。"""
    return next((name for name in names if _HAN.search(name) and not _KANA.search(name)), "")


class ActorMappingError(Exception):
    """没有可用的演员映射，或更新数据无效。"""


class ActorMapping:
    """一次构建、整批复用的名称索引；冲突名称不会选取任意条目。"""

    def __init__(self):
        self._japanese: dict[str, set[tuple[str, str]]] = {}
        self._primary: dict[str, set[tuple[str, str]]] = {}
        self._aliases: dict[str, set[tuple[str, str]]] = {}
        self.row_count = 0

    @classmethod
    def from_xml(cls, content: bytes):
        if len(content) > MAX_MAPPING_SIZE:
            raise ActorMappingError("演员映射文件过大")
        try:
            parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
            root = etree.fromstring(content, parser)
        except etree.XMLSyntaxError as error:
            raise ActorMappingError(f"演员映射 XML 无效: {error}") from error
        if root.tag != "actor-mapping" or root.getroottree().docinfo.doctype:
            raise ActorMappingError("演员映射根节点无效或含有 DTD")
        mapping = cls()
        for row in root.findall(".//a"):
            jp = (row.get("jp") or "").strip()
            if not jp:
                continue
            cn = (row.get("zh_cn") or "").strip()
            tw = (row.get("zh_tw") or "").strip()
            chinese = _chinese_name(cn, tw)
            display = f"{jp}（{chinese}）" if chinese and chinese != jp else jp
            actor = (jp, chinese)
            mapping._japanese.setdefault(jp, set()).add(actor)
            for name in (cn, tw, display):
                if name:
                    mapping._primary.setdefault(name, set()).add(actor)
            for name in (row.get("keyword") or "").split(","):
                if name.strip():
                    mapping._aliases.setdefault(name.strip(), set()).add(actor)
            mapping.row_count += 1
        if not mapping.row_count:
            raise ActorMappingError("演员映射没有包含原名的有效条目")
        return mapping

    @classmethod
    def from_file(cls, path: Path):
        try:
            if path.stat().st_size > MAX_MAPPING_SIZE:
                raise ActorMappingError("演员映射文件过大")
            return cls.from_xml(path.read_bytes())
        except OSError as error:
            raise ActorMappingError(f"无法读取演员映射缓存 {path}: {error}") from error

    def resolve(self, name: str, *, normalize_aliases=True) -> str:
        key = name.strip()
        for index in (self._japanese, self._primary, self._aliases):
            candidates = index.get(key)
            if candidates:
                originals = {jp for jp, _ in candidates}
                if len(originals) != 1:
                    return name
                jp = next(iter(originals))
                # 同一原名的占位记录不应遮住有效译名；所有入口复用相同的译名选择。
                translations = {cn for _, cn in self._japanese[jp] if cn and cn != jp}
                if len(translations) > 1:
                    return name
                chinese = next(iter(translations), jp if (jp, jp) in self._japanese[jp] else "")
                if not chinese:
                    for alias in _VERIFIED_CHINESE_ALIASES.get(jp, ()):
                        owners = {
                            owner
                            for names in (self._japanese, self._primary, self._aliases)
                            for owner, _ in names.get(alias, ())
                        }
                        if alias in self._aliases and owners == {jp}:
                            chinese = alias
                            break
                original = key if index is self._aliases and not normalize_aliases else jp
                return f"{original}（{chinese}）" if chinese and chinese != original else original
        return name


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
    cached = None
    cached_content = b""
    metadata_path = path.with_suffix(".json")
    etag = None
    try:
        if path.stat().st_size > MAX_MAPPING_SIZE:
            raise ActorMappingError("演员映射文件过大")
        cached_content = path.read_bytes()
        cached = ActorMapping.from_xml(cached_content)
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
        updated = ActorMapping.from_xml(content)
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
