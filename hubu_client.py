#!/usr/bin/env python3
"""
hubu_client.py - Hubu.cloud 上传客户端（走网页内部接口）

背景：hubu.cloud 的官方 API (/api/v2) 被站方关闭，WebDAV 的 PUT 是空实现
（返回 201 但文件不落盘）。本脚本复用浏览器登录态，直接调用文件管理器用的
上传接口，实现可用的上传/建目录/删除。

用法：
    export HUBU_COOKIE='filehosting=xxxxxxxx'
    # 可选，仅 ls 需要（WebDAV 只读，本身工作正常）
    export HUBU_DAV_USER='kali'
    export HUBU_DAV_PASS='xxxxxxxx'

    python3 hubu_client.py upload ./a.txt            # 传到根目录
    python3 hubu_client.py upload ./a.txt --folder 54865
    python3 hubu_client.py mkdir 新目录名 --parent 0
    python3 hubu_client.py folders                    # 列出目录 id
    python3 hubu_client.py ls                        # 用 WebDAV 列根目录
    python3 hubu_client.py ls /kali
    python3 hubu_client.py rm --file-id 902478
    python3 hubu_client.py rm --folder-id 54870

注意：
  * 会话 Cookie 失效后会提示重新登录。
  * 上传接口每次请求都需要新的 cTracker（脚本自动从 uploader.js 抓取）。
  * 单文件 >50MB 走分片（Content-Range），与网页行为一致。
  * 请勿高频并发调用，避免触发站方风控。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

SITE = "https://hubu.cloud"
UPLOAD_URL = SITE + "/ajax/file_upload_handler"
UPLOADER_JS = SITE + "/assets/js/uploader.js?r=1794804682"
FOLDER_CREATE_URL = SITE + "/account/ajax/add_edit_folder_process"
TRASH_URL = SITE + "/account/ajax/trash_files"
STATS_URL = SITE + "/account/ajax/get_account_file_stats"
LOAD_FILES_URL = SITE + "/account/ajax/load_files"
FILE_DETAILS_URL = SITE + "/account/ajax/file_details"
DAV_URL = SITE + "/webdav"

CHUNK_SIZE = 50_000_000  # 与 uploader.js 的 maxChunkSize 一致
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class HubuError(RuntimeError):
    pass


class HubuClient:
    def __init__(self, cookie: str, dav_user: str = "", dav_pass: str = ""):
        self.cookie = cookie.strip()
        if not self.cookie.startswith("filehosting="):
            self.cookie = "filehosting=" + self.cookie
        self.session_id = self.cookie.split("=", 1)[1].split(";")[0]
        self.dav_user = dav_user
        self.dav_pass = dav_pass
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": UA,
                "Cookie": self.cookie,
                "Origin": SITE,
                "Referer": SITE + "/account",
            }
        )

    # ---------- 基础 ----------

    def _get_ctracker(self) -> str:
        r = self.s.get(UPLOADER_JS, timeout=30)
        m = re.search(r"cTracker:\s*'([0-9a-f]{32})'", r.text)
        if not m:
            raise HubuError(
                "拿不到 cTracker，Cookie 可能已失效：请重新登录后更新 HUBU_COOKIE"
            )
        return m.group(1)

    def _check_login(self) -> None:
        r = self.s.get(SITE + "/account", timeout=30)
        if "/account/login" in r.url or "Captcha" in r.text[:2000]:
            raise HubuError("登录态无效：请重新登录浏览器，更新 HUBU_COOKIE")

    # ---------- 目录 ----------

    def folders(self) -> dict[str, str]:
        """返回 {folder_id: folder_name}（只含用户目录，不含虚拟目录）。"""
        r = self.s.post(
            STATS_URL,
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=30,
        )
        r.raise_for_status()
        import json

        data = json.loads(r.text)
        raw = data.get("folderArray") or "{}"
        return json.loads(raw)

    def list_folder(self, node_id: str = "-1") -> list[dict]:
        """列出目录内容（复用网页文件管理器的 load_files 接口）。

        返回 [{"id": "902486", "name": "a.txt", "is_dir": False}, ...]
        node_id="-1" 表示根目录；子目录传 folder id。
        """
        r = self.s.post(
            LOAD_FILES_URL,
            data={
                "pageType": "folder",
                "nodeId": str(node_id),
                "pageStart": "1",
                "perPage": "0",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=60,
        )
        r.raise_for_status()
        try:
            page = r.json().get("html") or ""
        except ValueError:
            raise HubuError(f"列目录返回非 JSON：{r.text[:200]}")
        out = []
        # 文件：<div ... fileId="902486" ... dttitle="name" ...>
        for m in re.finditer(r'<div[^>]*\bfileId="(\d+)"[^>]*>', page):
            tag = m.group(0)
            title = re.search(r'dttitle="([^"]*)"', tag)
            out.append(
                {
                    "id": m.group(1),
                    "name": unescape(title.group(1)) if title else "",
                    "is_dir": False,
                }
            )
        # 目录：<div ... id="folderItem54871" ... folderId="54871" ...>
        # 名字在后面的 <span class="filename">dav-dir</span>
        for m in re.finditer(r'<div[^>]*\bid="folderItem(\d+)"[^>]*>', page):
            name = re.search(r'class="filename">([^<]*)<', page[m.end() :])
            out.append(
                {
                    "id": m.group(1),
                    "name": unescape(name.group(1)) if name else "",
                    "is_dir": True,
                }
            )
        return out

    def resolve_path(self, path: str) -> tuple[str, str] | None:
        """把 /a/b/c 解析成 ("file"|"dir", 远程 id)。根目录返回 ("dir", "")。"""
        path = path.strip("/")
        if not path:
            return ("dir", "")
        node = "-1"
        kind = "dir"
        for part in path.split("/"):
            try:
                entries = self.list_folder(node)
            except Exception:
                return None
            match = next((e for e in entries if e["name"] == part), None)
            if not match:
                return None
            node = match["id"]
            kind = "dir" if match["is_dir"] else "file"
        return (kind, node)

    def file_read_url(self, file_id: str, name: str = "f") -> str | None:
        """取带 download_token 的直链。

        网页上传的文件在 WebDAV 上读不出内容（GET 返回 0 字节），只能用这个直链；
        该直链支持 Range，适合视频拖动。
        """
        r = self.s.post(
            FILE_DETAILS_URL,
            data={"u": str(file_id), "p": "true"},
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=60,
        )
        r.raise_for_status()
        try:
            page = r.json().get("html") or ""
        except ValueError:
            return None
        m = re.search(
            r"https://[^/'\"]+/([0-9a-f]{32})/[^'\"\s<]*?\?download_token=([0-9a-f]{64})",
            page,
        )
        if not m:
            return None
        # ww1.hubu.cloud 对账号文件返回 404，换回主域即可
        return f"https://hubu.cloud/{m.group(1)}/{quote(name)}?download_token={m.group(2)}"

    def mkdir(self, name: str, parent_id: str = "0") -> str:
        r = self.s.post(
            FOLDER_CREATE_URL,
            data={
                "folderName": name,
                "parentId": parent_id,
                "isPublic": "0",
                "submitme": "1",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=60,
        )
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            raise HubuError(f"建目录失败，返回非 JSON：{r.text[:200]}")
        if not data.get("success"):
            raise HubuError(f"建目录失败：{data.get('msg')}")
        return str(data.get("folder_id", ""))

    def trash(self, file_ids=(), folder_ids=()) -> dict:
        r = self.s.post(
            TRASH_URL,
            data={
                "fileIds": ",".join(str(i) for i in file_ids),
                "folderIds": ",".join(str(i) for i in folder_ids),
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise HubuError(f"删除失败：{data.get('msg')}")
        return data

    # ---------- 上传 ----------

    def upload(
        self, local_path: str | Path, folder_id: str = "", name: str | None = None
    ) -> dict:
        path = Path(local_path)
        if not path.is_file():
            raise HubuError(f"文件不存在：{path}")
        total = path.stat().st_size
        remote_name = name or path.name
        ctracker = self._get_ctracker()

        def fields() -> dict:
            return {
                "_sessionid": self.session_id,
                "cTracker": ctracker,
                "maxChunkSize": str(CHUNK_SIZE),
                "folderId": folder_id or "",
                "uploadSource": "file_manager",
            }

        if total <= CHUNK_SIZE:
            with path.open("rb") as fh:
                r = self.s.post(
                    UPLOAD_URL,
                    data=fields(),
                    files={"files[]": (remote_name, fh, "application/octet-stream")},
                    timeout=(30, 3600),
                )
            return self._parse_upload(r)

        results = []
        with path.open("rb") as fh:
            start = 0
            while start < total:
                chunk = fh.read(CHUNK_SIZE)
                end = start + len(chunk) - 1
                r = self.s.post(
                    UPLOAD_URL,
                    data=fields(),
                    files={
                        "files[]": (remote_name, chunk, "application/octet-stream")
                    },
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{total}",
                    },
                    timeout=(30, 3600),
                )
                if r.status_code != 200:
                    raise HubuError(
                        f"分片 {start}-{end} 上传失败：HTTP {r.status_code} {r.text[:200]}"
                    )
                if end == total - 1:
                    results = self._parse_upload(r)
                start = end + 1
        return results

    def _parse_upload(self, r: requests.Response) -> dict:
        if r.status_code != 200:
            raise HubuError(f"上传失败：HTTP {r.status_code} {r.text[:300]}")
        import json

        try:
            data = json.loads(r.text)
        except ValueError:
            raise HubuError(f"上传返回非 JSON：{r.text[:300]}")
        if isinstance(data, list):
            data = data[0] if data else {}
        if data.get("error"):
            raise HubuError(f"上传失败：{data.get('error')}")
        return data

    # ---------- 只读（走 WebDAV，本身是正常的）----------

    def ls(self, dav_path: str = "/") -> list[dict]:
        if not (self.dav_user and self.dav_pass):
            raise HubuError("ls 需要 HUBU_DAV_USER / HUBU_DAV_PASS（WebDAV 账号）")
        url = DAV_URL + (dav_path if dav_path.startswith("/") else "/" + dav_path)
        if not url.endswith("/"):
            url += "/"
        r = requests.request(
            "PROPFIND",
            url,
            auth=(self.dav_user, self.dav_pass),
            headers={"Depth": "1", "Content-Type": "text/xml"},
            data=(
                '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
                "<d:displayname/><d:resourcetype/><d:getcontentlength/>"
                "<d:getlastmodified/></d:prop></d:propfind>"
            ),
            timeout=60,
        )
        if r.status_code != 207:
            raise HubuError(f"PROPFIND 失败：HTTP {r.status_code}")
        ns = {"d": "DAV:"}
        out = []
        base = urlparse(url).path
        for resp in ET.fromstring(r.content).findall("d:response", ns):
            href = resp.findtext("d:href", default="", namespaces=ns)
            name = href.rstrip("/").rsplit("/", 1)[-1]
            is_dir = resp.find(".//d:collection", ns) is not None
            size = resp.findtext(".//d:getcontentlength", default="", namespaces=ns)
            mtime = resp.findtext(".//d:getlastmodified", default="", namespaces=ns)
            if href == base or not name:
                continue
            out.append(
                {
                    "name": name,
                    "href": href,
                    "is_dir": is_dir,
                    "size": int(size) if size.isdigit() else 0,
                    "modified": mtime,
                }
            )
        return out


def _client() -> HubuClient:
    cookie = os.environ.get("HUBU_COOKIE", "").strip()
    if not cookie:
        sys.exit("请先设置 HUBU_COOKIE（浏览器 Cookie 里的 filehosting 值）")
    return HubuClient(
        cookie,
        os.environ.get("HUBU_DAV_USER", ""),
        os.environ.get("HUBU_DAV_PASS", ""),
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Hubu.cloud 上传客户端（网页内部接口）")
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("upload", help="上传文件")
    up.add_argument("file")
    up.add_argument("--folder", default="", help="目标目录 id（默认根目录）")

    mk = sub.add_parser("mkdir", help="新建目录")
    mk.add_argument("name")
    mk.add_argument("--parent", default="0")

    ls = sub.add_parser("ls", help="列出目录（走 WebDAV）")
    ls.add_argument("path", nargs="?", default="/")

    rm = sub.add_parser("rm", help="删除文件/目录")
    rm.add_argument("--file-id", action="append", default=[])
    rm.add_argument("--folder-id", action="append", default=[])

    sub.add_parser("folders", help="列出目录 id")

    args = p.parse_args(argv)
    c = _client()

    if args.cmd == "upload":
        info = c.upload(args.file, args.folder)
        print(
            f"OK  {info.get('name')}  id={info.get('file_id')}  "
            f"{info.get('size')}B  {info.get('url')}"
        )
    elif args.cmd == "mkdir":
        fid = c.mkdir(args.name, args.parent)
        print(f"OK  folder_id={fid}")
    elif args.cmd == "ls":
        for e in c.ls(args.path):
            kind = "d" if e["is_dir"] else "-"
            print(f"{kind} {e['size']:>12}  {e['name']}")
    elif args.cmd == "rm":
        if not (args.file_id or args.folder_id):
            sys.exit("请给 --file-id 或 --folder-id")
        print(c.trash(args.file_id, args.folder_id))
    elif args.cmd == "folders":
        for fid, name in c.folders().items():
            print(f"{fid}\t{name}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HubuError as e:
        sys.exit(f"错误：{e}")
