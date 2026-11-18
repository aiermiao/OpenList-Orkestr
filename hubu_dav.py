#!/usr/bin/env python3
"""
hubu_dav.py — 本地 WebDAV 网关（读写 hubu.cloud）

原理：
  * 读（PROPFIND）  → 代理到 hubu.cloud 自己的 WebDAV
  * 读（GET）       → 老文件走 WebDAV；网页接口上传的文件在 WebDAV 上
                      读不出来（返回 0 字节），改用文件详情接口给出的
                      带 download_token 的直链（支持 Range，可拖进度条）
  * 写（PUT / MKCOL）→ 走网页文件管理器的内部接口（见 hubu_client.py）
  * 删（DELETE）     → 走 /account/ajax/trash_files，需要数字 id
  * MOVE / COPY      → 用「读源 + 上传目标 + 删源」实现

目录/文件 id 会自动解析（走网页文件管理器的 load_files 接口），
不需要再手动 map；早期的 map / map-file 命令仍保留，作为兜底覆盖。

启动：
    export HUBU_COOKIE='filehosting=xxxxxxxx'
    export HUBU_DAV_USER='kali'
    export HUBU_DAV_PASS='xxxxxxxx'
    .venv-dav/bin/python hubu_dav.py serve

    # 默认监听 127.0.0.1:8080，客户端地址：
    #   Finder:        http://127.0.0.1:8080/
    #   资源管理器:     http://127.0.0.1:8080/
    #   rclone:        rclone serve / rclone mount 用 webdav remote
    #   Nautilus:      dav://127.0.0.1:8080/

已知限制（重要）：
  1. MOVE 大文件 = 重新上传一遍，移动 10GB 会花 10GB 的流量。
  2. 写操作依赖网页登录 Cookie（HUBU_COOKIE），过期后需要重新登录更新。
  3. 网关自己的索引在 ~/.config/hubu/index.db；删除这个文件只会丢缓存，
     不会影响云端数据。
"""

from __future__ import annotations

import argparse
import email.utils
import mimetypes
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from wsgidav.dav_error import DAVError, HTTP_FORBIDDEN
from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider
from wsgidav.wsgidav_app import WsgiDAVApp

from hubu_client import SITE, HubuClient, HubuError

# 兼容修复：Python 3.12+ / 3.14 下客户端断开或 GC 时 cheroot makefile 抛出 Bad file descriptor 的日志噪点
try:
    import cheroot.makefile

    _orig_cheroot_flush = cheroot.makefile.BufferedWriter._flush_unlocked

    def _safe_cheroot_flush(self):
        try:
            return _orig_cheroot_flush(self)
        except OSError:
            if hasattr(self, "_write_buf"):
                self._write_buf.clear()

    cheroot.makefile.BufferedWriter._flush_unlocked = _safe_cheroot_flush
except Exception:
    pass

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _new_session() -> requests.Session:
    """带重试的会话。上游 nginx 偶尔会直接掐断 TLS 连接。"""
    s = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        status=2,
        backoff_factor=0.4,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=None,  # PROPFIND 等自定义方法也重试
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry, pool_connections=8, pool_maxsize=32, pool_block=False
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

def _clean_env(name: str, default: str = "") -> str:
    val = os.environ.get(name, default)
    if val is None:
        return default
    val = val.strip()
    if (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
        val = val[1:-1].strip()
    return val


DAV_ROOT = SITE + "/webdav"
CFG_DIR = Path(_clean_env("HUBU_CFG_DIR", str(Path.home() / ".config" / "hubu")))
DB_PATH = CFG_DIR / "index.db"


# --------------------------------------------------------------------------
# 本地索引：path -> remote id
# --------------------------------------------------------------------------
def _norm(path: str) -> str:
    return path.rstrip("/") or "/"


class Index:
    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS nodes ("
            " path TEXT PRIMARY KEY,"
            " kind TEXT NOT NULL,"
            " remote_id TEXT,"
            " delete_hash TEXT,"
            " read_url TEXT,"
            " updated REAL)"
        )
        # 兼容旧库
        cols = {row[1] for row in self.db.execute("PRAGMA table_info(nodes)")}
        if "read_url" not in cols:
            self.db.execute("ALTER TABLE nodes ADD COLUMN read_url TEXT")
        self.db.commit()

    def get(self, path: str) -> dict | None:
        path = _norm(path)
        with self.lock:
            row = self.db.execute(
                "SELECT path, kind, remote_id, delete_hash, read_url, updated"
                " FROM nodes WHERE path=?",
                (path,),
            ).fetchone()
        if not row:
            return None
        return {
            "path": row[0],
            "kind": row[1],
            "remote_id": row[2],
            "delete_hash": row[3],
            "read_url": row[4],
            "updated": row[5],
        }

    def put(self, path, kind, remote_id=None, delete_hash=None, read_url=None):
        path = _norm(path)
        with self.lock:
            self.db.execute(
                "INSERT INTO nodes(path, kind, remote_id, delete_hash, read_url, updated)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(path) DO UPDATE SET kind=excluded.kind,"
                " remote_id=COALESCE(excluded.remote_id, nodes.remote_id),"
                " delete_hash=COALESCE(excluded.delete_hash, nodes.delete_hash),"
                " read_url=COALESCE(excluded.read_url, nodes.read_url),"
                " updated=excluded.updated",
                (
                    path,
                    kind,
                    str(remote_id) if remote_id else None,
                    delete_hash,
                    read_url,
                    time.time(),
                ),
            )
            self.db.commit()

    def forget(self, path):
        path = _norm(path)
        with self.lock:
            self.db.execute("DELETE FROM nodes WHERE path=?", (path,))
            self.db.execute("DELETE FROM nodes WHERE path LIKE ?", (path.rstrip("/") + "/%",))
            self.db.commit()

    def forget_tree(self, path):
        self.forget(path)


# --------------------------------------------------------------------------
# 上游 WebDAV（只读）
# --------------------------------------------------------------------------
def _split(path):
    path = path.rstrip("/")
    if not path:
        return "/", ""
    parent, _, name = path.rpartition("/")
    return (parent or "/"), name


class Upstream:
    def __init__(self, user, password, cookie: str = "", ttl=20):
        self.s = _new_session()
        self.s.auth = (user, password)
        self.s.headers["User-Agent"] = UA
        if cookie:
            if not cookie.startswith("filehosting="):
                cookie = "filehosting=" + cookie
            self.s.headers["Cookie"] = cookie
        self.ttl = ttl
        self._cache = {}
        self._ranges = {}
        self._lock = threading.Lock()

    def _url(self, path):
        if not path.startswith("/"):
            path = "/" + path
        return DAV_ROOT + quote(path, safe="/")

    def _propfind(self, path, depth):
        try:
            r = self.s.request(
                "PROPFIND",
                self._url(path),
                headers={"Depth": str(depth), "Content-Type": "text/xml"},
                data=(
                    '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
                    "<d:resourcetype/><d:getcontentlength/><d:getetag/>"
                    "<d:getlastmodified/><d:getcontenttype/>"
                    "</d:prop></d:propfind>"
                ),
                timeout=60,
            )
        except Exception as e:
            err = f"PROPFIND {path} 连接上游失败: {e}"
            print(f"[Hubu Upstream Error] {err}", file=sys.stderr, flush=True)
            raise HubuError(err)

        if r.status_code == 404:
            return []
        if r.status_code != 207:
            preview = r.text[:300].replace("\n", " ").strip()
            err = f"PROPFIND {path} 失败：HTTP {r.status_code} ({preview})"
            print(f"[Hubu Upstream Error] {err}", file=sys.stderr, flush=True)
            raise HubuError(err)
        return _parse_multistatus(r.content, path)

    def listdir(self, path):
        now = time.time()
        with self._lock:
            hit = self._cache.get(path)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        me = path.rstrip("/") or "/"
        entries = {
            e["name"]: e
            for e in self._propfind(path, 1)
            if e["name"] and e.get("path", "").rstrip("/") != me
        }
        with self._lock:
            self._cache[path] = (now, entries)
        return entries

    def stat(self, path):
        if path in ("", "/"):
            return {
                "name": "",
                "is_dir": True,
                "size": 0,
                "etag": None,
                "mtime": None,
                "ctype": "httpd/unix-directory",
            }
        parent, name = _split(path)
        return self.listdir(parent).get(name)

    def invalidate(self, path=None):
        with self._lock:
            if path is None:
                self._cache.clear()
            else:
                self._cache.pop(path, None)
                self._cache.pop(_split(path)[0], None)

    def supports_ranges(self, path):
        """上游对某些文件（网页上传的）会 500，探测一次并缓存。"""
        now = time.time()
        with self._lock:
            hit = self._ranges.get(path)
            if hit and now - hit[1] < 300:
                return hit[0]
        t0 = time.time()
        try:
            r = self.s.get(
                self._url(path),
                headers={"Range": "bytes=0-1"},
                stream=True,
                timeout=30,
            )
            cr = r.headers.get("Content-Range", "")
            ok = r.status_code == 206 and cr.startswith("bytes 0-1/")
            r.close()
        except Exception:
            # 探测失败多半是上游偶发断连；先按支持 Range 处理，
            # 真正读取时 HttpRangeFile 会自动重试。
            ok = True
        with self._lock:
            self._ranges[path] = (ok, now)
        return ok

    def open_range(self, path, size, ranged=True, block=None):
        if block is None:
            block = int(_clean_env("HUBU_BLOCK_SIZE", str(4 * 1024 * 1024)))
        return HttpRangeFile(self.s, self._url(path), size, block=block, ranged=ranged)


def _parse_multistatus(body, base_path):
    import xml.etree.ElementTree as ET

    ns = {"d": "DAV:"}
    out = []
    for resp in ET.fromstring(body).findall("d:response", ns):
        href = resp.findtext("d:href", default="", namespaces=ns)
        decoded = unquote(href)
        if decoded.startswith("/webdav"):
            decoded = decoded[len("/webdav"):] or "/"
        if not decoded.endswith("/"):
            decoded = decoded.rstrip("/")
        name = decoded.rstrip("/").rsplit("/", 1)[-1] if decoded not in ("", "/") else ""
        is_dir = resp.find(".//d:collection", ns) is not None
        size = resp.findtext(".//d:getcontentlength", default="", namespaces=ns)
        etag = resp.findtext(".//d:getetag", default="", namespaces=ns)
        ctype = resp.findtext(".//d:getcontenttype", default="", namespaces=ns)
        mtime = resp.findtext(".//d:getlastmodified", default="", namespaces=ns)
        ts = None
        if mtime:
            try:
                ts = email.utils.parsedate_to_datetime(mtime).timestamp()
            except Exception:
                ts = None
        out.append(
            {
                "path": decoded,
                "name": name,
                "is_dir": is_dir,
                "size": int(size) if size.isdigit() else 0,
                "etag": etag.strip('"') if etag else None,
                "ctype": ctype or None,
                "mtime": ts,
            }
        )
    return out


class GlobalBlockCache:
    """全局跨请求的 Range 块缓存（LRU）。

    浏览器与流媒体播放器（如 Firefox / VLC）在串流时会发起数百次独立的 HTTP Range 请求
    （每次通常仅读 512KB ~ 1MB）。如果每次请求都创建独立的缓冲区，会导致未读完的
    预拉取数据被频繁销毁、上游频繁被轰炸。
    本缓存跨请求常驻内存，保证 4MB 块只向上游拉取一次，后续请求 0ms 内存级直接命中。
    """

    def __init__(self, block_size: int = 4 * 1024 * 1024, max_blocks: int = 12):
        self.block_size = block_size
        self.max_blocks = max_blocks
        self._cache = {}
        self._order = []
        self._lock = threading.Lock()
        self._fetching = {}  # (url, block_idx) -> threading.Event

    def get(self, url: str, block_idx: int) -> bytes | None:
        with self._lock:
            key = (url, block_idx)
            if key in self._cache:
                self._order.remove(key)
                self._order.append(key)
                return self._cache[key]
            return None

    def start_fetch(self, url: str, block_idx: int) -> bool:
        """返回 True 表示当前线程获得拉取权；返回 False 表示已有其他线程在拉取，需等待。"""
        with self._lock:
            key = (url, block_idx)
            if key in self._cache:
                return False
            if key in self._fetching:
                return False
            evt = threading.Event()
            self._fetching[key] = evt
            return True

    def wait_fetch(self, url: str, block_idx: int, timeout: float = 60.0) -> bytes | None:
        with self._lock:
            key = (url, block_idx)
            evt = self._fetching.get(key)
        if evt:
            evt.wait(timeout)
        return self.get(url, block_idx)

    def finish_fetch(self, url: str, block_idx: int, data: bytes | None):
        with self._lock:
            key = (url, block_idx)
            if data is not None:
                if key in self._cache:
                    self._order.remove(key)
                elif len(self._cache) >= self.max_blocks:
                    oldest = self._order.pop(0)
                    self._cache.pop(oldest, None)
                self._cache[key] = data
                self._order.append(key)
            evt = self._fetching.pop(key, None)
            if evt:
                evt.set()


_GLOBAL_BLOCK_CACHE = GlobalBlockCache(
    block_size=int(_clean_env("HUBU_BLOCK_SIZE", str(4 * 1024 * 1024))),
    max_blocks=int(_clean_env("HUBU_CACHE_BLOCKS", "12")),  # 12 * 4MB = 48MB RAM
)


class HttpRangeFile:
    """用 HTTP Range 请求实现的只读文件对象，支持 seek（视频拖动进度条）。"""

    def __init__(self, session, url, size, block=None, ranged=True):
        if block is None:
            block = _GLOBAL_BLOCK_CACHE.block_size
        # 每个文件单独一个会话：避免多线程共用一个连接池时被上游
        # 掐断连接后互相拖累，也方便失败时整体重建连接。
        self._auth = getattr(session, "auth", None)
        self._base_headers = dict(session.headers)
        self.session = _new_session()
        self.session.auth = self._auth
        self.session.headers.update(self._base_headers)
        self.url = url
        self.size = int(size or 0)
        self.block = block
        self.ranged = ranged
        self.pos = 0
        self._resp = None

    def _reopen(self):
        try:
            self.session.close()
        except Exception:
            pass
        self.session = _new_session()
        self.session.auth = self._auth
        self.session.headers.update(self._base_headers)

    def seek(self, offset, whence=0):
        if whence == 0:
            self.pos = offset
        elif whence == 1:
            self.pos += offset
        else:
            self.pos = self.size + offset
        self.pos = max(0, min(self.pos, self.size))
        return self.pos

    def tell(self):
        return self.pos

    def _ensure_stream(self):
        if self._resp is None:
            last = None
            for attempt in range(5):
                try:
                    r = self.session.get(
                        self.url,
                        stream=True,
                        headers={"Accept-Encoding": "identity"},
                        timeout=(20, 300),
                    )
                    r.raise_for_status()
                    self._resp = r
                    return
                except Exception as e:
                    last = e
                    print(f"[Hubu Stream Warning] 确保流式连接重试 ({attempt+1}/5): {e}", file=sys.stderr, flush=True)
                    self._reopen()
                    time.sleep(0.5 * (attempt + 1))
            raise IOError(f"读取失败：{last}")

    def _get_range(self, start, end):
        last = None
        for attempt in range(5):
            try:
                r = self.session.get(
                    self.url,
                    headers={
                        "Range": f"bytes={start}-{end}",
                        "Accept-Encoding": "identity",
                    },
                    timeout=(20, 180),
                )
                if r.status_code in (200, 206):
                    return r
                last = IOError(f"读取失败：HTTP {r.status_code}")
                try:
                    r.close()
                except Exception:
                    pass
            except Exception as e:
                last = e
            print(f"[Hubu Stream Warning] bytes {start}-{end} 重试 ({attempt+1}/5): {last}", file=sys.stderr, flush=True)
            self._reopen()
            time.sleep(0.5 * (attempt + 1))
        print(f"[Hubu Stream Error] bytes {start}-{end} 最终失败: {last}", file=sys.stderr, flush=True)
        raise IOError(f"读取失败：{last}")

    def _load_block(self, b: int) -> bytes:
        data = _GLOBAL_BLOCK_CACHE.get(self.url, b)
        if data is not None:
            return data

        if not _GLOBAL_BLOCK_CACHE.start_fetch(self.url, b):
            data = _GLOBAL_BLOCK_CACHE.wait_fetch(self.url, b)
            if data is not None:
                return data

        b_size = _GLOBAL_BLOCK_CACHE.block_size
        b_start = b * b_size
        b_end = min((b + 1) * b_size, self.size) - 1
        try:
            r = self._get_range(b_start, b_end)
            data = r.content
            try:
                r.close()
            except Exception:
                pass
            _GLOBAL_BLOCK_CACHE.finish_fetch(self.url, b, data)
            return data
        except Exception:
            _GLOBAL_BLOCK_CACHE.finish_fetch(self.url, b, None)
            raise

    def read(self, n=-1):
        if not self.ranged:
            self._ensure_stream()
            try:
                data = self._resp.raw.read(
                    n if (n is not None and n >= 0) else None
                )
            except Exception:
                if self.pos != 0:
                    raise
                self._resp = None
                self._ensure_stream()
                data = self._resp.raw.read(
                    n if (n is not None and n >= 0) else None
                )
            self.pos += len(data or b"")
            return data

        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""

        req_start = self.pos
        req_end = req_start + n
        b_size = _GLOBAL_BLOCK_CACHE.block_size
        first_b = req_start // b_size
        last_b = (req_end - 1) // b_size

        parts = []
        for b in range(first_b, last_b + 1):
            data = self._load_block(b)
            b_start = b * b_size
            b_end = b_start + len(data)
            slice_start = max(req_start, b_start) - b_start
            slice_end = min(req_end, b_end) - b_start
            if slice_end > slice_start:
                parts.append(data[slice_start:slice_end])

        res = b"".join(parts)
        self.pos += len(res)
        return res

    def close(self):
        if self._resp is not None:
            self._resp.close()
            self._resp = None
        try:
            self.session.close()
        except Exception:
            pass
        try:
            self.session.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------
class HubuProvider(DAVProvider):
    def __init__(self, up: Upstream, idx: Index, wc: HubuClient):
        super().__init__()
        self.up = up
        self.idx = idx
        self.wc = wc
        self.pending = {}  # path -> expires_at（LOCK 用的占位文件）
        self.lock = threading.Lock()

    # ---- id 解析 ----
    def folder_id(self, path):
        """返回上传接口需要的 folderId。根目录返回 ''。"""
        path = path.rstrip("/") or "/"
        if path == "/":
            return ""
        rec = self.idx.get(path)
        if rec and rec["kind"] == "dir" and rec["remote_id"]:
            return rec["remote_id"]
        # 自动用网页列表接口逐级解析（旧目录无需手动 map）
        try:
            parsed = self.wc.resolve_path(path)
        except Exception:
            parsed = None
        if parsed and parsed[0] == "dir":
            self.idx.put(path, "dir", parsed[1])
            return parsed[1]
        # 顶层目录兜底：folderArray
        parent, name = _split(path)
        if parent == "/":
            try:
                for fid, fname in self.wc.folders().items():
                    if fname == name:
                        self.idx.put(path, "dir", fid)
                        return fid
            except Exception:
                pass
        return None

    def file_id(self, path):
        """返回文件在网页接口里的 file_id（删除/读直链需要）。"""
        rec = self.idx.get(path)
        if rec and rec["kind"] == "file" and rec["remote_id"]:
            return rec["remote_id"]
        try:
            parsed = self.wc.resolve_path(path)
        except Exception:
            parsed = None
        if parsed and parsed[0] == "file":
            self.idx.put(path, "file", parsed[1])
            return parsed[1]
        return None

    def read_url(self, path):
        """网页上传的文件只能用带 token 的直链读；旧文件返回 None，走 WebDAV。"""
        rec = self.idx.get(path)
        if rec and rec.get("read_url"):
            # "-" 表示探测过：该文件只能走 WebDAV
            if rec["read_url"] == "-":
                return None
            if time.time() - (rec.get("updated") or 0) < 3600:
                return rec["read_url"]
            # 直链有有效期，过期后重新解析
        fid = (rec or {}).get("remote_id") or self.file_id(path)
        if not fid:
            return None
        try:
            url = self.wc.file_read_url(fid, _basename(path))
        except Exception:
            url = None
        if url and not self._url_works(url):
            # 老文件虽然有 download_token，但直链是 404，只能回退 WebDAV
            url = None
        self.idx.put(path, "file", fid, read_url=url or "-")
        return url

    def _url_works(self, url):
        try:
            r = self.wc.s.get(
                url, headers={"Range": "bytes=0-1"}, stream=True, timeout=30
            )
            ok = r.status_code in (200, 206)
            r.close()
            return ok
        except Exception:
            return False

    # ---- 资源解析 ----
    def get_resource_inst(self, path, environ):
        if path in ("", "/"):
            return HubuCollection("/", environ)
        against = self.pending.get(path)
        if against:
            if against > time.time():
                return HubuFile(path, environ, info=None, exists=False)
            self.pending.pop(path, None)
        info = self.up.stat(path)
        if not info:
            # 上游 WebDAV 对新目录有延迟；本地索引里已知是目录就直接认
            rec = self.idx.get(path)
            if rec and rec.get("kind") == "dir":
                return HubuCollection(path, environ)
            return None
        if info["is_dir"]:
            return HubuCollection(path, environ)
        return HubuFile(path, environ, info=info)

    def mark_pending(self, path):
        with self.lock:
            self.pending[path] = time.time() + 600

    def clear_pending(self, path):
        with self.lock:
            self.pending.pop(path, None)


def _basename(path):
    return path.rstrip("/").rsplit("/", 1)[-1] or "/"


class HubuCollection(DAVCollection):
    def get_display_name(self):
        return _basename(self.path)

    def get_member_names(self):
        names = list(self.provider.up.listdir(self.path).keys())
        # 叠加本地索引：刚由网关创建、上游 WebDAV 还没列出来的子目录
        prefix = self.path.rstrip("/") + "/"
        with self.provider.idx.lock:
            rows = self.provider.idx.db.execute(
                "SELECT path FROM nodes WHERE path LIKE ? AND kind='dir'",
                (prefix + "%",),
            ).fetchall()
        for (p,) in rows:
            rel = p[len(prefix) :]
            if rel and "/" not in rel and rel not in names:
                names.append(rel)
        return names

    def get_member(self, name):
        from wsgidav import util

        return self.provider.get_resource_inst(
            util.join_uri(self.path, name), self.environ
        )

    def support_recursive_delete(self):
        return True

    def create_empty_resource(self, name):
        from wsgidav import util

        path = util.join_uri(self.path, name)
        self.provider.mark_pending(path)
        return HubuFile(path, self.environ, info=None, exists=False)

    def create_collection(self, name):
        from wsgidav import util

        if self.provider.folder_id(self.path) is None:
            raise DAVError(
                HTTP_FORBIDDEN,
                f"无法解析目录 id：{self.path}",
            )
        path = util.join_uri(self.path, name)
        try:
            fid = self.provider.wc.mkdir(name, self.provider.folder_id(self.path) or "0")
        except HubuError as e:
            raise DAVError(HTTP_FORBIDDEN, str(e))
        self.provider.idx.put(path, "dir", fid)
        self.provider.up.invalidate(self.path)
        return HubuCollection(path, self.environ)

    def delete(self):
        fid = self.provider.folder_id(self.path)
        if not fid:
            raise DAVError(
                HTTP_FORBIDDEN, f"无法解析目录 id：{self.path}"
            )
        try:
            self.provider.wc.trash(folder_ids=[fid])
        except HubuError as e:
            raise DAVError(HTTP_FORBIDDEN, str(e))
        self.provider.idx.forget_tree(self.path)
        self.provider.up.invalidate(_split(self.path)[0])

    def copy_move_single(self, dest_path, *, is_move):
        from wsgidav import util

        parent = self.provider.get_resource_inst(
            util.get_uri_parent(dest_path), self.environ
        )
        if parent is None:
            raise DAVError(HTTP_FORBIDDEN, "目标父目录不存在")
        parent.create_collection(util.get_uri_name(dest_path))


class HubuFile(DAVNonCollection):
    def __init__(self, path, environ, info=None, exists=True):
        super().__init__(path, environ)
        self.info = info or {}
        self.exists = exists
        self._tmp_path = None

    # ---- 元数据 ----
    def get_content_length(self):
        return int(self.info.get("size") or 0)

    def get_content_type(self):
        if self.info.get("ctype"):
            return self.info["ctype"]
        return mimetypes.guess_type(self.path)[0] or "application/octet-stream"

    def get_etag(self):
        if self.info.get("etag"):
            return self.info["etag"]
        return f"{self.info.get('size') or 0}-{int(self.info.get('mtime') or 0)}"

    def support_etag(self):
        return True

    def support_recursive_move(self, dest_path):
        return False

    def support_ranges(self):
        if not self.exists or self.get_content_length() == 0:
            return False
        if self.provider.read_url(self.path):
            return True
        return self.provider.up.supports_ranges(self.path)

    def get_last_modified(self):
        return self.info.get("mtime") or super().get_last_modified()

    # ---- 读 ----
    def get_content(self):
        if not self.exists:
            return HttpRangeFile(self.provider.up.s, "", 0)
        block = int(_clean_env("HUBU_BLOCK_SIZE", str(4 * 1024 * 1024)))
        url = self.provider.read_url(self.path)
        if url:
            return HttpRangeFile(
                self.provider.wc.s, url, self.get_content_length(), block=block, ranged=True
            )
        return self.provider.up.open_range(
            self.path,
            self.get_content_length(),
            block=block,
            ranged=self.provider.up.supports_ranges(self.path),
        )

    # ---- 写 ----
    def begin_write(self, *, content_type=None):
        fd, tmp = tempfile.mkstemp(prefix="hubu-", dir="/tmp")
        os.close(fd)
        self._tmp_path = tmp
        return open(tmp, "wb")

    def end_write(self, *, with_errors):
        tmp = self._tmp_path
        self._tmp_path = None
        if with_errors or not tmp:
            if tmp:
                os.unlink(tmp)
            self.provider.clear_pending(self.path)
            return
        parent, _ = _split(self.path)
        folder_id = self.provider.folder_id(parent)
        if folder_id is None:
            os.unlink(tmp)
            self.provider.clear_pending(self.path)
            raise DAVError(
                HTTP_FORBIDDEN,
                f"无法解析目录 id：{parent}",
            )
        try:
            info = self.provider.wc.upload(tmp, folder_id, name=_basename(self.path))
        except HubuError as e:
            raise DAVError(HTTP_FORBIDDEN, str(e))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        name = _basename(self.path)
        try:
            read_url = self.provider.wc.file_read_url(info.get("file_id"), name)
        except Exception:
            read_url = None
        self.provider.idx.put(
            self.path,
            "file",
            info.get("file_id"),
            info.get("delete_hash"),
            read_url=read_url,
        )
        self.provider.up.invalidate(parent)
        self.provider.clear_pending(self.path)
        self.info = {
            "size": int(info.get("size") or 0),
            "etag": None,
            "mtime": time.time(),
            "ctype": info.get("type"),
        }
        self.exists = True

    # ---- 删 ----
    def delete(self):
        rec = self.provider.idx.get(self.path)
        parent, _ = _split(self.path)
        fid = (rec or {}).get("remote_id") or self.provider.file_id(self.path)
        try:
            if fid:
                self.provider.wc.trash(file_ids=[fid])
            elif rec and rec.get("delete_hash"):
                _delete_by_hash(self.path, rec)
            else:
                raise DAVError(
                    HTTP_FORBIDDEN,
                    f"找不到 {self.path} 的 file_id，无法删除",
                )
        except HubuError as e:
            raise DAVError(HTTP_FORBIDDEN, str(e))
        self.provider.idx.forget(self.path)
        self.provider.up.invalidate(parent)

    # ---- 复制/移动 ----
    def copy_move_single(self, dest_path, *, is_move):
        from wsgidav import util

        dest_parent_path = util.get_uri_parent(dest_path)
        parent = self.provider.get_resource_inst(dest_parent_path, self.environ)
        if parent is None:
            raise DAVError(HTTP_FORBIDDEN, "目标父目录不存在")
        folder_id = self.provider.folder_id(dest_parent_path)
        if folder_id is None:
            raise DAVError(
                HTTP_FORBIDDEN, f"无法解析目标目录 id：{dest_parent_path}"
            )
        fd, tmp = tempfile.mkstemp(prefix="hubu-copy-", dir="/tmp")
        try:
            with os.fdopen(fd, "wb") as out:
                src = self.get_content()
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                src.close()
            try:
                info = self.provider.wc.upload(
                    tmp, folder_id, name=_basename(dest_path)
                )
            except HubuError as e:
                raise DAVError(HTTP_FORBIDDEN, str(e))
            name = _basename(dest_path)
            try:
                read_url = self.provider.wc.file_read_url(info.get("file_id"), name)
            except Exception:
                read_url = None
            self.provider.idx.put(
                dest_path,
                "file",
                info.get("file_id"),
                info.get("delete_hash"),
                read_url=read_url,
            )
            self.provider.up.invalidate(dest_parent_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def _delete_by_hash(path, rec):
    # 兜底：匿名上传的公开文件可以用 delete_hash 删除
    r = requests.post(
        f"https://ww1.hubu.cloud/{path}~d?{rec['delete_hash']}",
        data={"submitted": "1"},
        timeout=60,
    )
    if r.status_code != 200:
        raise HubuError(f"按 delete_hash 删除失败：HTTP {r.status_code}")


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def build_app(host, port, local_user, local_pass):
    cookie = _clean_env("HUBU_COOKIE")
    dav_user = _clean_env("HUBU_DAV_USER")
    dav_pass = _clean_env("HUBU_DAV_PASS")
    local_user = local_user.strip().strip("'\"")
    local_pass = local_pass.strip().strip("'\"")
    if not cookie:
        sys.exit("请先 export HUBU_COOKIE='filehosting=...'")
    if not (dav_user and dav_pass):
        sys.exit("请先 export HUBU_DAV_USER / HUBU_DAV_PASS（上游 WebDAV 账号）")

    print(
        f"[Hubu Init] 上游账号: {dav_user}, 密码长度: {len(dav_pass)}, "
        f"Cookie长度: {len(cookie)}, 本地用户: {local_user}",
        flush=True,
    )

    wc = HubuClient(cookie, dav_user, dav_pass)
    idx = Index()
    up = Upstream(dav_user, dav_pass, cookie=cookie)
    provider = HubuProvider(up, idx, wc)

    config = {
        "host": host,
        "port": port,
        "provider_mapping": {"/": provider},
        "simple_dc": {"user_mapping": {"*": {local_user: {"password": local_pass}}}},
        "http_authenticator": {
            "domain_controller": None,
            "accept_basic": True,
            "accept_digest": False,
            "default_to_digest": False,
        },
        "verbose": 1,
        "dir_browser": {"enable": False},
        "block_size": 1 << 16,
    }
    return WsgiDAVApp(config)


def main(argv=None):
    p = argparse.ArgumentParser(description="hubu.cloud 本地 WebDAV 网关")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="启动网关")
    s.add_argument("--host", default=_clean_env("HUBU_HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(_clean_env("HUBU_PORT", "8080")))
    s.add_argument(
        "--user", default=_clean_env("HUBU_LOCAL_USER", _clean_env("HUBU_DAV_USER", "hubu"))
    )
    s.add_argument(
        "--pass",
        dest="password",
        default=_clean_env("HUBU_LOCAL_PASS", _clean_env("HUBU_DAV_PASS", "")),
    )

    m = sub.add_parser("map", help="注册目录 id")
    m.add_argument("path")
    m.add_argument("remote_id")

    mf = sub.add_parser("map-file", help="注册文件 id")
    mf.add_argument("path")
    mf.add_argument("remote_id")

    d = sub.add_parser("unmap", help="删除本地映射")
    d.add_argument("path")

    sub.add_parser("show", help="查看本地映射")

    args = p.parse_args(argv)
    idx = Index()

    if args.cmd == "serve":
        if not args.password:
            sys.exit("请给 --pass 或设置 HUBU_LOCAL_PASS/HUBU_DAV_PASS")
        app = build_app(args.host, args.port, args.user, args.password)
        print(
            f"WebDAV 网关已启动： http://{args.host}:{args.port}/\n"
            f"  用户：{args.user}\n"
            f"  索引：{DB_PATH}\n"
            f"  读走上游 WebDAV，写走网页内部接口。Ctrl+C 退出。",
            flush=True,
        )
        from cheroot import wsgi

        server = wsgi.Server((args.host, args.port), app)
        try:
            server.start()
        except KeyboardInterrupt:
            server.stop()
        return 0

    if args.cmd == "map":
        path = "/" + args.path.strip("/")
        idx.put(path, "dir", args.remote_id)
        print(f"OK  {path} -> dir id {args.remote_id}")
    elif args.cmd == "map-file":
        path = "/" + args.path.strip("/")
        idx.put(path, "file", args.remote_id)
        print(f"OK  {path} -> file id {args.remote_id}")
    elif args.cmd == "unmap":
        path = "/" + args.path.strip("/")
        idx.forget(path)
        print(f"OK  removed {path}")
    elif args.cmd == "show":
        with idx.lock:
            rows = idx.db.execute(
                "SELECT path, kind, remote_id FROM nodes ORDER BY path"
            ).fetchall()
        for path, kind, rid in rows:
            print(f"{kind:4} {rid or '-':>10}  {path}")
        if not rows:
            print("（空）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HubuError as e:
        sys.exit(f"错误：{e}")
