"""R18.dev 转储的原子导入与本地只读查询。"""

import gzip
import hashlib
import io
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from javsp.config import Cfg
from javsp.datatype import MovieInfo
from javsp.download import DownloadError, DownloadSpec, clear_parts, download_file

logger = logging.getLogger(__name__)
BUCKET_URL = "https://r18dotdev.s3.eu-west-1.wasabisys.com/"
_DUMP_PREFIX = "dumps/r18dotdev_dump_"
_DUMP_NAME = re.compile(r"dumps/r18dotdev_dump_(\d{4}-\d{2}-\d{2})\.sql\.gz")
_XML_NS = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
SOURCE = "r18dev"
TABLES = {
    "derived_video",
    "derived_actress",
    "derived_video_actress",
    "derived_category",
    "derived_video_category",
    "derived_director",
    "derived_video_director",
    "derived_maker",
    "derived_label",
    "derived_series",
}
ENTITY_TABLES = (
    "derived_actress",
    "derived_category",
    "derived_director",
    "derived_maker",
    "derived_label",
    "derived_series",
)
MAPPABLE_FIELDS = (
    "title",
    "plot",
    "cover",
    "big_cover",
    "genre",
    "serial",
    "actress",
    "director",
    "duration",
    "producer",
    "publisher",
    "publish_date",
    "preview_pics",
    "preview_video",
)
_COPY = re.compile(r"^COPY public\.(\w+) \(([^)]+)\) FROM stdin;$")
_ESCAPE = re.compile(r"\\([\\nrtbfv])")
_ESCAPES = {"\\": "\\", "n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v"}


@dataclass(frozen=True)
class DumpObject:
    url: str
    dump_date: date
    etag: str
    size: int


def _date_from_url(url: str) -> date | None:
    match = re.search(r"(?:^|/)r18dotdev_dump_(\d{4}-\d{2}-\d{2})\.sql\.gz$", urlparse(url).path)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _latest_dump(session, options: dict) -> DumpObject:
    latest = None
    token = None
    seen_tokens = set()
    while True:
        params = {"list-type": "2", "prefix": _DUMP_PREFIX, "max-keys": "1000"}
        if token is not None:
            params["continuation-token"] = token
        with session.get(BUCKET_URL, params=params, timeout=(10, 60), **options) as response:
            response.raise_for_status()
            root = ET.fromstring(response.content)
        if root.tag != f"{{{_XML_NS['s']}}}ListBucketResult":
            raise ValueError("Wasabi 对象列表格式异常")
        for item in root.findall("s:Contents", _XML_NS):
            key = item.findtext("s:Key", default="", namespaces=_XML_NS)
            match = _DUMP_NAME.fullmatch(key)
            if match is None:
                continue
            try:
                dump_date = date.fromisoformat(match.group(1))
                size = int(item.findtext("s:Size", default="", namespaces=_XML_NS))
            except ValueError:
                continue
            etag = item.findtext("s:ETag", default="", namespaces=_XML_NS)
            if size <= 0 or not re.fullmatch(r'"[A-Za-z0-9-]+"', etag):
                continue
            candidate = DumpObject(BUCKET_URL + quote(key, safe="/"), dump_date, etag, size)
            if latest is None or candidate.dump_date > latest.dump_date:
                latest = candidate
        if root.findtext("s:IsTruncated", default="false", namespaces=_XML_NS) != "true":
            break
        token = root.findtext("s:NextContinuationToken", namespaces=_XML_NS)
        if not token or token in seen_tokens:
            raise ValueError("Wasabi 对象列表分页标记异常")
        seen_tokens.add(token)
    if latest is None:
        raise ValueError("Wasabi 对象列表中没有有效的 R18.dev 转储")
    return latest


def _local_version(path: Path) -> tuple[date | None, str | None, int | None]:
    with closing(_connect(path)) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(dump_meta)")}
        fields = [field for field in ("source_url", "dump_date", "etag", "size") if field in columns]
        row = dict(zip(fields, db.execute(f"SELECT {', '.join(fields)} FROM dump_meta").fetchone(), strict=True))
    try:
        local_date = date.fromisoformat(row["dump_date"]) if row.get("dump_date") else _date_from_url(row["source_url"])
    except ValueError:
        local_date = None
    return local_date, row.get("etag"), row.get("size")


def _decode(value):
    if value == r"\N":
        return None
    return _ESCAPE.sub(lambda match: _ESCAPES[match.group(1)], value)


def _connect(path):
    return sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)


def _valid(path):
    if not path.is_file():
        return False
    try:
        with closing(_connect(path)) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return False
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not TABLES.issubset(tables) or "dump_meta" not in tables:
                return False
            return (
                db.execute("SELECT COUNT(*) FROM derived_video").fetchone()[0] > 0
                and db.execute("SELECT source_url FROM dump_meta").fetchone() is not None
            )
    except (sqlite3.Error, OSError):
        return False


def _import_dump(stream, path, source: DumpObject):
    """只识别 pg_dump 的 COPY 数据块，不执行转储中的 SQL。"""
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA journal_mode=OFF")
        db.execute("PRAGMA synchronous=OFF")
        db.execute(
            "CREATE TABLE dump_meta (source_url TEXT NOT NULL, dump_date TEXT NOT NULL, "
            "etag TEXT NOT NULL, size INTEGER NOT NULL)"
        )
        current = None
        columns = []
        pending = []
        insert_sql = ""
        seen = set()

        def flush():
            if pending:
                db.executemany(insert_sql, pending)
                pending.clear()

        for line in stream:
            line = line.rstrip("\r\n")
            if current is not None:
                if line == r"\.":
                    flush()
                    current = None
                    continue
                if not insert_sql:
                    continue
                values = line.split("\t")
                if len(values) != len(columns):
                    raise ValueError(f"R18.dev 转储表 {current} 列数不匹配")
                pending.append(tuple(_decode(value) for value in values))
                if len(pending) >= 1000:
                    flush()
                continue
            match = _COPY.match(line)
            if not match:
                continue
            current, names = match.groups()
            columns = [name.strip() for name in names.split(",")]
            insert_sql = ""
            if current in TABLES:
                if current in seen or not all(re.fullmatch(r"[a-z_]+", name) for name in columns):
                    raise ValueError(f"R18.dev 转储表 {current} 格式异常")
                seen.add(current)
                db.execute(f"CREATE TABLE {current} ({', '.join(name + ' TEXT' for name in columns)})")
                insert_sql = f"INSERT INTO {current} VALUES ({', '.join('?' for _ in columns)})"
        if current is not None or seen != TABLES:
            raise ValueError("R18.dev 转储不完整或表结构已变化")
        required = {
            "derived_video": {
                "content_id",
                "dvd_id",
                "title_ja",
                "title_en",
                "comment_ja",
                "comment_en",
                "runtime_mins",
                "release_date",
                "sample_url",
                "maker_id",
                "label_id",
                "series_id",
                "jacket_full_url",
                "jacket_thumb_url",
                "gallery_full_first",
                "gallery_full_last",
            },
            "derived_actress": {"id", "name_kanji", "name_romaji", "image_url"},
            "derived_video_actress": {"content_id", "actress_id", "ordinality"},
            "derived_category": {"id", "name_ja", "name_en"},
            "derived_video_category": {"content_id", "category_id"},
            "derived_director": {"id", "name_kanji", "name_romaji"},
            "derived_video_director": {"content_id", "director_id"},
            "derived_maker": {"id", "name_ja", "name_en"},
            "derived_label": {"id", "name_ja", "name_en"},
            "derived_series": {"id", "name_ja", "name_en"},
        }
        for table, names in required.items():
            present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if not names.issubset(present):
                raise ValueError(f"R18.dev 转储表 {table} 缺少所需字段")
        if db.execute("SELECT COUNT(*) FROM derived_video").fetchone()[0] == 0:
            raise ValueError("R18.dev 转储没有影片数据")
        db.execute("CREATE INDEX video_dvd ON derived_video (UPPER(REPLACE(REPLACE(dvd_id, '-', ''), ' ', '')))")
        db.execute("CREATE INDEX video_cid ON derived_video (content_id)")
        for table in ("derived_video_actress", "derived_video_category", "derived_video_director"):
            db.execute(f"CREATE INDEX {table}_cid ON {table} (content_id)")
        # 关联名称及制作信息按实体 id 查询；这些表在转储中没有主键。
        for table in ENTITY_TABLES:
            db.execute(f"CREATE INDEX {table}_id ON {table} (id)")
        db.execute(
            "INSERT INTO dump_meta VALUES (?, ?, ?, ?)",
            (source.url, source.dump_date.isoformat(), source.etag, source.size),
        )
        db.commit()
    finally:
        db.close()


def _ensure_entity_indexes(path):
    """为旧版已导入的数据库补建查询索引；失败时仍保留可用旧库。"""
    try:
        with sqlite3.connect(path) as db:
            present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            missing = [table for table in ENTITY_TABLES if f"{table}_id" not in present]
            if not missing:
                return
            db.execute("BEGIN")
            for table in missing:
                db.execute(f"CREATE INDEX {table}_id ON {table} (id)")
        logger.info("R18.dev 本地数据库查询索引已更新")
    except (OSError, sqlite3.Error) as error:
        logger.warning("R18.dev 本地数据库查询索引更新失败，继续使用原数据库: %s", error)


def prepare_database(path: Path, online_enabled: bool, *, session=None):
    """启动时检查最新版本；失败时只允许使用有效旧库或在线抓取。"""
    path = Path(path)
    usable = _valid(path)
    if path.exists() and not usable:
        logger.warning("R18.dev 本地数据库无效，将重新下载: %s", path)
    if usable:
        _ensure_entity_indexes(path)
    client = session or requests.Session()
    proxy = Cfg().network.proxy_server
    options = {"proxies": {"http": str(proxy), "https": str(proxy)}} if proxy is not None else {}
    temporary = path.with_name(path.name + ".tmp")
    archive = None
    download = None
    try:
        source = _latest_dump(client, options)
        if usable:
            old_date, old_etag, old_size = _local_version(path)
            if old_date and (
                old_date > source.dump_date
                or (old_date == source.dump_date and old_etag == source.etag and old_size == source.size)
            ):
                logger.info("R18.dev 本地数据库已是最新版本: %s", old_date)
                return path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)
        identity = hashlib.sha256(f"{source.url}\0{source.size}\0{source.etag}".encode()).hexdigest()[:24]
        download = DownloadSpec(source.url, source.size, path.parent / f".r18dev-{source.dump_date}-{identity}", source.etag)
        logger.info("正在下载并导入 R18.dev 数据库: %s", source.url)
        archive = download_file(download, client=client, options=options, label="下载 R18.dev 数据库", logger=logger)
        with gzip.open(archive, "rb") as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8") as stream:
                _import_dump(stream, temporary, source)
        if not _valid(temporary):
            raise ValueError("R18.dev 本地数据库校验失败")
        temporary.replace(path)
        clear_parts(download)
        logger.info("R18.dev 本地数据库已更新: %s", path)
        return path
    except (
        OSError,
        EOFError,
        UnicodeError,
        sqlite3.Error,
        ValueError,
        ET.ParseError,
        requests.RequestException,
        DownloadError,
    ) as error:
        if download is not None and (archive is not None or isinstance(error, DownloadError)):
            clear_parts(download)
        logger.warning("R18.dev 数据库更新失败: %s", error)
        if usable:
            return path
        if online_enabled:
            logger.warning("本次运行退回在线抓取")
            return None
        raise RuntimeError("R18.dev 数据库不可用，且在线抓取已关闭") from error
    finally:
        if session is None:
            client.close()
        if archive is not None:
            archive.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def _url(value):
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    value = value.lstrip("/")
    if not re.search(r"\.(?:jpe?g|png|webp)$", value, re.I):
        value += ".jpg"
    return "https://pics.dmm.co.jp/" + value


def _gallery(first, last):
    if not first:
        return None
    if not last or first == last:
        return [_url(first)]
    start = re.match(r"^(.*-)(\d+)$", first)
    end = re.match(r"^(.*-)(\d+)$", last)
    if not start or not end or start.group(1) != end.group(1):
        return [_url(first)]
    low, high = int(start.group(2)), int(end.group(2))
    if high < low or high - low > 100:
        return [_url(first)]
    return [_url(f"{start.group(1)}{index}") for index in range(low, high + 1)]


def _name(db, table, relation, entity, content_id, fields):
    query = f"SELECT {fields} FROM {relation} AS r JOIN {table} AS e ON e.id = r.{entity}_id WHERE r.content_id = ?"
    if relation == "derived_video_actress":
        query += " ORDER BY r.ordinality"
    return db.execute(query, (content_id,)).fetchall()


def lookup(path: Path, movie):
    """精确匹配 CID 或规范化番号；歧义记录交由在线站点处理。"""
    with closing(_connect(Path(path))) as db:
        db.row_factory = sqlite3.Row
        if movie.cid and (movie.data_src == "cid" or not movie.dvdid):
            rows = db.execute("SELECT * FROM derived_video WHERE content_id = ? LIMIT 2", (movie.cid,)).fetchall()
        elif movie.dvdid:
            normalized = re.sub(r"[-\s]", "", movie.dvdid).upper()
            rows = db.execute(
                "SELECT * FROM derived_video WHERE UPPER(REPLACE(REPLACE(dvd_id, '-', ''), ' ', '')) = ? LIMIT 2", (normalized,)
            ).fetchall()
        else:
            return None
        if len(rows) != 1:
            if rows:
                logger.warning("R18.dev 本地库番号有多条记录: %s", movie.dvdid or movie.cid)
            return None
        row = rows[0]
        cid = row["content_id"]
        actress_rows = _name(
            db, "derived_actress", "derived_video_actress", "actress", cid, "e.name_kanji, e.name_romaji, e.image_url"
        )
        category_rows = _name(db, "derived_category", "derived_video_category", "category", cid, "e.name_ja, e.name_en")
        director_rows = _name(db, "derived_director", "derived_video_director", "director", cid, "e.name_kanji, e.name_romaji")

        def entity(table, key):
            if not row[key]:
                return None
            item = db.execute(f"SELECT name_ja, name_en FROM {table} WHERE id = ?", (row[key],)).fetchone()
            return (item[0] or item[1]) if item else None

        actresses = [(item[0] or item[1]) for item in actress_rows if item[0] or item[1]]
        info = MovieInfo(movie)
        info.dvdid = row["dvd_id"] or info.dvdid
        info.cid = cid
        info.title = row["title_ja"] or row["title_en"]
        info.plot = row["comment_ja"] or row["comment_en"]
        info.cover = _url(row["jacket_thumb_url"] or row["jacket_full_url"])
        info.big_cover = _url(row["jacket_full_url"])
        info.genre = [(item[0] or item[1]) for item in category_rows if item[0] or item[1]] or None
        info.actress = actresses or None
        info.actress_pics = {
            (item[0] or item[1]): _url(item[2]) for item in actress_rows if (item[0] or item[1]) and item[2]
        } or None
        info.director = next((item[0] or item[1] for item in director_rows if item[0] or item[1]), None)
        info.duration = str(row["runtime_mins"]) if row["runtime_mins"] else None
        info.publish_date = row["release_date"]
        info.preview_video = row["sample_url"]
        info.preview_pics = _gallery(row["gallery_full_first"], row["gallery_full_last"])
        info.producer = entity("derived_maker", "maker_id")
        info.publisher = entity("derived_label", "label_id")
        info.serial = entity("derived_series", "series_id")
        return info


def needs_online(info):
    return info is None or any(not getattr(info, field) for field in MAPPABLE_FIELDS)
