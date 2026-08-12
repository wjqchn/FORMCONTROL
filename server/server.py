#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
质量文控平台 · 本地数据服务

职责：
  - 在同一 origin (http://127.0.0.1:PORT) 下托管安装目录中的 表格分类汇总.html
  - 提供 /api/* 接口，把业务数据落盘到安装路径下的 data.sqlite（真实文件）
  - 业务数据与文件指针完全不依赖浏览器存储；清除任何浏览数据均不影响

启动方式（由安装包桌面快捷方式调用）：
  FormControl.exe "C:\Users\Public\FORMCONTROL"
其中参数为应用安装目录，服务会把 data.sqlite 与读写都放在该目录。
若未传参，则默认使用 exe 自身所在目录。
"""

import sys
import os
import re
import json
import sqlite3
import socket
import threading
import webbrowser
import urllib.request
import time
import ctypes
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

INDEX_FILE = "表格分类汇总.html"
DB_FILE = "data.sqlite"
DEFAULT_PORT = 5178
APP_NAME = "质量文控平台"
MUTEX_NAME = "FormControlAppMutex"

# ---------------------------------------------------------------------------
# 应用目录与数据库
# ---------------------------------------------------------------------------
def resolve_app_dir():
    # 从 sys.argv 中提取非选项参数作为安装目录；过滤 --shutdown 等选项
    plain_args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if plain_args:
        d = plain_args[0]
        # 兼容 Git Bash / WSL 传入的 /c/... 或 /mnt/c/... 形式
        m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", d)
        if not m:
            m = re.match(r"^/([a-zA-Z])/(.*)$", d)
        if m:
            d = m.group(1) + ":/" + m.group(2)
        d = os.path.abspath(d)
        os.makedirs(d, exist_ok=True)
        return d
    # 否则使用 exe/脚本所在目录（开发或直接双击 exe 时）
    return os.path.abspath(os.path.dirname(sys.executable))


APP_DIR = resolve_app_dir()
DB_PATH = os.path.join(APP_DIR, DB_FILE)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, data TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn


def load_records():
    conn = get_conn()
    try:
        rows = conn.execute("SELECT data FROM records").fetchall()
        return [json.loads(r[0]) for r in rows]
    finally:
        conn.close()


def save_records(arr):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM records")
        for r in arr:
            rid = r.get("id") or ""
            conn.execute(
                "INSERT OR REPLACE INTO records (id, data) VALUES (?, ?)",
                (rid, json.dumps(r, ensure_ascii=False)),
            )
        conn.commit()
    finally:
        conn.close()


def load_settings():
    conn = get_conn()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        out = {}
        for k, v in rows:
            try:
                out[k] = json.loads(v)
            except Exception:
                out[k] = v
        return out
    finally:
        conn.close()


def save_settings(obj):
    conn = get_conn()
    try:
        for k, v in obj.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (k, json.dumps(v, ensure_ascii=False)),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 静态文件 MIME
# ---------------------------------------------------------------------------
MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
}


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "FormControlServer/1.0"

    def log_message(self, *args):
        pass  # 静默日志，避免控制台刷屏

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_text(self, code, text, ctype="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/api/health":
            return self._send_json(200, {"ok": True})
        if path == "/api/records":
            return self._send_json(200, load_records())
        if path == "/api/settings":
            return self._send_json(200, load_settings())
        return self.serve_static(path)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}

        if path == "/api/records":
            if not isinstance(payload, list):
                return self._send_json(400, {"error": "expected array"})
            save_records(payload)
            return self._send_json(200, {"ok": True, "count": len(payload)})

        if path == "/api/settings":
            if not isinstance(payload, dict):
                return self._send_json(400, {"error": "expected object"})
            save_settings(payload)
            return self._send_json(200, {"ok": True})

        if path == "/api/quit":
            self._send_json(200, {"ok": True})
            # 在独立线程中关闭服务，避免在请求线程内死锁
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        return self._send_json(404, {"error": "not found"})

    def serve_static(self, path):
        if path in ("/", ""):
            path = "/" + INDEX_FILE
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(APP_DIR, rel))
        # 防目录穿越
        if not full.startswith(APP_DIR) or not os.path.isfile(full):
            return self._send_text(404, "not found")
        # 禁止直接下载数据库文件
        if os.path.basename(full) == DB_FILE:
            return self._send_text(403, "forbidden")
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except Exception:
            return self._send_text(404, "not found")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------------------
# 互斥体：让安装/卸载程序能识别本服务正在运行
# ---------------------------------------------------------------------------
def create_app_mutex():
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW(None, False, MUTEX_NAME)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 远程关闭：向运行中的本服务实例发送 /api/quit
# ---------------------------------------------------------------------------
def shutdown_existing():
    for port in range(DEFAULT_PORT, DEFAULT_PORT + 50):
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/quit",
                data=b"{}",
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=1) as resp:
                if resp.status == 200:
                    time.sleep(0.5)  # 给服务一点时间释放文件与端口
                    return
        except Exception:
            continue


# ---------------------------------------------------------------------------
# 端口探测：优先使用空闲端口；若端口被「本服务」占用则复用并只打开浏览器
# ---------------------------------------------------------------------------
def probe_port(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.close()
        return "free"
    except OSError:
        pass
    # 端口被占用：探活判断是否是我们自己的另一个实例
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=1
        ) as resp:
            if resp.status == 200:
                return "reuse"
    except Exception:
        pass
    return "busy"


def find_port(start):
    for port in range(start, start + 50):
        status = probe_port(port)
        if status in ("free", "reuse"):
            return status, port
    return None, None


def open_browser(port):
    url = f"http://127.0.0.1:{port}/"
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main():
    # --shutdown：仅优雅关闭已运行实例，不启动服务
    if "--shutdown" in sys.argv:
        shutdown_existing()
        return

    # 创建互斥体，便于安装/卸载程序识别本进程
    create_app_mutex()

    status, port = find_port(DEFAULT_PORT)
    if port is None:
        sys.stderr.write("无法找到可用端口，启动失败。\n")
        return
    if status == "reuse":
        # 已有实例在运行，仅打开浏览器
        open_browser(port)
        return
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    # 确保数据库文件已就绪
    get_conn().close()
    # 略微延迟后自动打开浏览器，避免页面尚未就绪
    threading.Timer(0.6, open_browser, args=(port,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
