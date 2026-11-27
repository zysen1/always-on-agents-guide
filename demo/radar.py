#!/usr/bin/env python3
"""本地选题雷达。只使用 Python 标准库，未批准的选题不会进入成稿清单。"""
import argparse
import json
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree

DEFAULT_DB = Path.home() / ".always-on-radar" / "radar.sqlite3"
DEFAULT_REPO = "langchain-ai/langgraph"
SAMPLE = {
    "title": "Building always-on agents",
    "source": "Anthropic",
    "url": "https://claude.com/resources/articles/building-always-on-agents",
}
KEYWORDS = ("agent", "langgraph", "智能体", "工作流", "workflow")


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
            source TEXT NOT NULL, state TEXT NOT NULL, draft TEXT NOT NULL,
            received_at TEXT NOT NULL, approved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY, stamp TEXT NOT NULL, action TEXT NOT NULL,
            item_id INTEGER NOT NULL
        );
    """)
    return conn


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(conn, action, item_id):
    conn.execute("INSERT INTO history(stamp,action,item_id) VALUES(?,?,?)",
                 (now(), action, item_id))
    conn.execute("DELETE FROM history WHERE id <= (SELECT MAX(id) - 200 FROM history)")


def receive(conn, event, keywords=KEYWORDS):
    title, url, source = (event[key].strip() for key in ("title", "url", "source"))
    uri = urlsplit(url)
    if not title or not source or uri.scheme not in ("https", "http") or not uri.hostname:
        raise ValueError("需要标题、来源名和有效的 http(s) 原文网址。")
    # Remove only the fragment; a query string may identify a distinct original source.
    url = uri._replace(fragment="").geturl()
    matched = not keywords or any(k.casefold() in title.casefold() for k in keywords)
    state = "pending" if matched else "skipped"
    draft = f"《{title}》值得为谁讲？读完 {source} 的原文，选一个观众能复现的例子，展示操作和结果。" if matched else ""
    with conn:
        cursor = conn.execute(
            "INSERT INTO items(url,title,source,state,draft,received_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(url) DO NOTHING", (url, title, source, state, draft, now()))
        item_id = conn.execute("SELECT id FROM items WHERE url=?", (url,)).fetchone()[0]
        action = "重复已跳过" if cursor.rowcount == 0 else ("生成待审草稿" if matched else "未命中关注词")
        log(conn, action, item_id)
    return {"action": action, "id": item_id}


def approve(conn, item_id):
    with conn:
        cursor = conn.execute("UPDATE items SET state='approved',approved_at=? WHERE id=? AND state='pending'",
                              (now(), item_id))
        if cursor.rowcount:
            log(conn, "人工确认收录", item_id)
            return {"action": "人工确认收录", "id": item_id}
        row = conn.execute("SELECT state FROM items WHERE id=?", (item_id,)).fetchone()
        if row and row[0] == "approved":
            return {"action": "此前已收录", "id": item_id}
        raise ValueError("找不到此编号的待审草稿。")


def snapshot(conn):
    items = [dict(row) for row in conn.execute("SELECT * FROM items ORDER BY id DESC")]
    history = [dict(row) for row in conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT 12")]
    return {"items": items, "history": history}


def repo_name(value):
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}", value):
        raise ValueError("仓库格式应为 owner/repo，如 astral-sh/uv。")
    if value.split("/")[1] in (".", ".."):
        raise ValueError("无效的仓库名。")
    return value


def github_url(url):
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.netloc.casefold() == "github.com"


class GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not github_url(newurl):
            raise ValueError("发行订阅重定向到了 GitHub 之外。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_live(repo):
    repo = repo_name(repo)
    feed = f"https://github.com/{repo}/releases.atom"
    req = Request(feed, headers={"User-Agent": "AlwaysOnRadar/1.0", "Accept": "application/atom+xml"})
    with build_opener(GitHubRedirects()).open(req, timeout=12) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("来源返回的数据过大。")
    source = "LangGraph Releases" if repo.casefold() == DEFAULT_REPO else f"{repo} Releases"
    prefix = f"/{repo.casefold()}/releases/"
    ns = "{http://www.w3.org/2005/Atom}"
    root = ElementTree.fromstring(data)
    events = []
    for entry in root.findall(ns + "entry")[:10]:
        link = next((el.get("href") for el in entry.findall(ns + "link")
                     if el.get("rel", "alternate") == "alternate"), None)
        title = entry.findtext(ns + "title")
        if title and link and github_url(link) and urlsplit(link).path.casefold().startswith(prefix):
            events.append({"source": source, "title": title, "url": link})
    if not events:
        raise ValueError("来源暂未返回可用的公开版本条目，请稍后重试。")
    return events


def poll(conn, repo=DEFAULT_REPO, keywords=KEYWORDS):
    return [receive(conn, event, keywords) for event in reversed(fetch_live(repo))]


class Panel(BaseHTTPRequestHandler):
    def __init__(self, *args, db, **kwargs):
        self.db = db
        super().__init__(*args, **kwargs)

    def write_json(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(body, ensure_ascii=False).encode())

    def do_GET(self):
        if not self.local_request():
            return self.write_json(403, {"error": "请从本地工作台访问。"})
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(Path(__file__).with_name("panel.html").read_bytes())
        elif self.path == "/state":
            with closing(connect(self.db)) as conn:
                self.write_json(200, snapshot(conn))
        else:
            self.send_error(404)

    def local_request(self):
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        return self.headers.get("Host") in hosts and (not origin or origin in {"http://" + h for h in hosts})

    def do_POST(self):
        if not self.local_request() or self.headers.get("X-Radar-Action") != "local":
            return self.write_json(403, {"error": "请从本地工作台操作。"})
        try:
            with closing(connect(self.db)) as conn:
                if self.path == "/sample":
                    result = receive(conn, SAMPLE)
                elif self.path == "/live":
                    results = poll(conn)
                    new = sum(r["action"] == "生成待审草稿" for r in results)
                    result = {"action": f"读取 {len(results)} 条原始更新，新建 {new} 份草稿"}
                elif self.path.startswith("/approve/"):
                    result = approve(conn, int(self.path.rsplit("/", 1)[1]))
                else:
                    return self.write_json(404, {"error": "页面不存在。"})
            self.write_json(200, result)
        except (ValueError, OSError, URLError, ElementTree.ParseError, sqlite3.Error) as exc:
            self.write_json(400, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 文件位置")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sample", help="回放已核对的真实来源样本（离线）")
    live = commands.add_parser("live", help="拉取一次 GitHub 仓库的公开发行记录")
    watch = commands.add_parser("watch", help="定时轮询发行记录，Ctrl-C 退出")
    watch.add_argument("--minutes", type=int, default=60)
    add = commands.add_parser("add", help="接收一条自己的公开来源")
    for command in (live, watch):
        command.add_argument("--repo", default=DEFAULT_REPO, help="GitHub 公开仓库，格式 owner/repo")
    for command in (live, watch, add):
        filters = command.add_mutually_exclusive_group()
        filters.add_argument("--keyword", action="append", help="筛选标题，重复传入表示任一匹配")
        filters.add_argument("--all", action="store_true", help="接收所有标题，包括纯版本号")
    for key in ("title", "url", "source"):
        add.add_argument("--" + key, required=True)
    confirm = commands.add_parser("approve", help="人工确认收录到本机成稿清单")
    confirm.add_argument("id", type=int)
    commands.add_parser("status", help="查看收件、待审和已收录数量")
    export = commands.add_parser("export", help="以 JSON 导出匹配的完整选题条目")
    export.add_argument("--state", choices=["pending", "approved", "skipped"])
    serve = commands.add_parser("serve", help="打开本机工作台")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if hasattr(args, "repo"):
        args.repo = repo_name(args.repo)
    if hasattr(args, "keyword"):
        keywords = [] if args.all else [word.strip() for word in (args.keyword or KEYWORDS)]
        if any(not word for word in keywords):
            parser.error("关键词不能为空。")
    if args.command == "serve":
        with ThreadingHTTPServer(("127.0.0.1", args.port), partial(Panel, db=args.db)) as http:
            print(f"打开 http://127.0.0.1:{http.server_address[1]}  ·  Ctrl-C 退出", flush=True)
            http.serve_forever()
        return
    with closing(connect(args.db)) as conn:
        if args.command == "watch":
            if args.minutes < 1:
                parser.error("轮询间隔至少 1 分钟")
            while True:
                try:
                    received = poll(conn, args.repo, keywords)
                    print(f"{now()} 检查 {len(received)} 条更新", flush=True)
                except (URLError, OSError, ValueError, ElementTree.ParseError) as exc:
                    print(f"读取失败，将在下轮重试：{exc}", file=sys.stderr, flush=True)
                time.sleep(60 * args.minutes)
        elif args.command == "sample":
            result = receive(conn, SAMPLE)
        elif args.command == "add":
            result = receive(conn, vars(args), keywords)
        elif args.command == "approve":
            result = approve(conn, args.id)
        elif args.command == "live":
            result = poll(conn, args.repo, keywords)
        else:
            rows = snapshot(conn)["items"]
            if args.command == "export":
                result = [r for r in rows if not args.state or r["state"] == args.state]
            else:
                result = {"已收件": len(rows), "待审核": sum(r["state"] == "pending" for r in rows),
                          "已收录": sum(r["state"] == "approved" for r in rows),
                          "未命中": sum(r["state"] == "skipped" for r in rows)}
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, sqlite3.Error, ElementTree.ParseError) as exc:
        sys.exit(f"操作失败：{exc}")
