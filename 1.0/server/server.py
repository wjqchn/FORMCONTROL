#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
质量文控平台 · 本地数据服务

职责：
  - 在同一 origin (http://127.0.0.1:PORT) 下托管安装目录中的 表格分类汇总.html
  - 提供 /api/* 接口，把业务数据落盘到安装路径下的 data.sqlite
  - 提供可配置的本地数据库快照、查询、恢复和用户确认后的过期清理
"""

import sys
import os
import re
import json
import sqlite3
import socket
import shutil
import tempfile
import threading
import webbrowser
import urllib.request
import time
import ctypes
from datetime import datetime, timedelta
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

INDEX_FILE = "表格分类汇总.html"
DB_FILE = "data.sqlite"
BACKUP_META_FILE = "backup.json"
BACKUP_SETTINGS_FILE = "backup_settings.json"
DEFAULT_PORT = 5178
APP_NAME = "质量文控平台"
MUTEX_NAME = "FormControlAppMutex"
BACKUP_LOCK = threading.RLock()
DEFAULT_BACKUP_SETTINGS = {
    "path": "D:\\文档管理数据库备份",
    "triggers": {"edit": True, "delete": True, "import": True, "restore": True},
    "retentionDays": 7,
    "cleanupThresholdGB": 1,
}


# ---------------------------------------------------------------------------
# 应用目录与数据库
# ---------------------------------------------------------------------------
def resolve_app_dir():
    plain_args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if plain_args:
        directory = plain_args[0]
        match = re.match(r"^/mnt/([a-zA-Z])/(.*)$", directory) or re.match(r"^/([a-zA-Z])/(.*)$", directory)
        if match:
            directory = match.group(1) + ":/" + match.group(2)
        directory = os.path.abspath(directory)
        os.makedirs(directory, exist_ok=True)
        return directory
    return os.path.abspath(os.path.dirname(sys.executable))


APP_DIR = resolve_app_dir()
DB_PATH = os.path.join(APP_DIR, DB_FILE)
BACKUP_SETTINGS_PATH = os.path.join(APP_DIR, BACKUP_SETTINGS_FILE)


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
        return [json.loads(row[0]) for row in rows]
    finally:
        conn.close()


def save_records(records):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM records")
        for record in records:
            record_id = record.get("id") or ""
            conn.execute(
                "INSERT OR REPLACE INTO records (id, data) VALUES (?, ?)",
                (record_id, json.dumps(record, ensure_ascii=False)),
            )
        conn.commit()
    finally:
        conn.close()


def load_settings():
    conn = get_conn()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        output = {}
        for key, value in rows:
            try:
                output[key] = json.loads(value)
            except Exception:
                output[key] = value
        return output
    finally:
        conn.close()


def save_settings(values):
    conn = get_conn()
    try:
        for key, value in values.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 数据库备份模块
# ---------------------------------------------------------------------------
def atomic_write_json(path, value):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tmp_", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def get_backup_settings():
    settings = json.loads(json.dumps(DEFAULT_BACKUP_SETTINGS))
    try:
        with open(BACKUP_SETTINGS_PATH, "r", encoding="utf-8") as file:
            stored = json.load(file)
        if isinstance(stored, dict):
            for key in ("path", "retentionDays", "cleanupThresholdGB"):
                if key in stored:
                    settings[key] = stored[key]
            if isinstance(stored.get("triggers"), dict):
                for trigger in settings["triggers"]:
                    if trigger in stored["triggers"]:
                        settings["triggers"][trigger] = bool(stored["triggers"][trigger])
    except (OSError, ValueError, TypeError):
        pass
    return settings


def validate_backup_settings(payload):
    if not isinstance(payload, dict):
        raise ValueError("备份设置格式无效")
    current = get_backup_settings()
    backup_path = str(payload.get("path", current["path"])).strip()
    if not backup_path:
        raise ValueError("请选择备份路径")
    backup_path = os.path.abspath(backup_path)
    try:
        retention_days = int(payload.get("retentionDays", current["retentionDays"]))
        threshold_gb = float(payload.get("cleanupThresholdGB", current["cleanupThresholdGB"]))
    except (TypeError, ValueError):
        raise ValueError("保留天数和清理提醒阈值必须为数字")
    if retention_days < 1 or retention_days > 36500:
        raise ValueError("备份保存天数应为 1 至 36500 天")
    if threshold_gb <= 0 or threshold_gb > 102400:
        raise ValueError("清理提醒阈值应大于 0 且不超过 102400 GB")

    triggers = current["triggers"].copy()
    supplied = payload.get("triggers")
    if isinstance(supplied, dict):
        for name in triggers:
            if name in supplied:
                triggers[name] = bool(supplied[name])
    return {
        "path": backup_path,
        "triggers": triggers,
        "retentionDays": retention_days,
        "cleanupThresholdGB": threshold_gb,
    }


def save_backup_settings(payload):
    settings = validate_backup_settings(payload)
    os.makedirs(settings["path"], exist_ok=True)
    atomic_write_json(BACKUP_SETTINGS_PATH, settings)
    return settings


def backup_root(settings=None):
    return os.path.abspath((settings or get_backup_settings())["path"])


def backup_metadata(folder):
    meta_path = os.path.join(folder, BACKUP_META_FILE)
    try:
        with open(meta_path, "r", encoding="utf-8") as file:
            metadata = json.load(file)
        if not isinstance(metadata, dict):
            metadata = {}
    except (OSError, ValueError, TypeError):
        metadata = {}

    database_path = os.path.join(folder, DB_FILE)
    metadata.setdefault("id", os.path.basename(folder))
    metadata.setdefault("createdAt", datetime.fromtimestamp(os.path.getmtime(folder)).isoformat(timespec="seconds"))
    metadata.setdefault("operation", "unknown")
    metadata["sizeBytes"] = os.path.getsize(database_path) if os.path.isfile(database_path) else 0
    return metadata


def list_backups():
    settings = get_backup_settings()
    root = backup_root(settings)
    if not os.path.isdir(root):
        return []
    cutoff = datetime.now() - timedelta(days=settings["retentionDays"])
    backups = []
    for entry in os.scandir(root):
        if not entry.is_dir(follow_symlinks=False):
            continue
        if not os.path.isfile(os.path.join(entry.path, DB_FILE)):
            continue
        metadata = backup_metadata(entry.path)
        try:
            created = datetime.fromisoformat(str(metadata["createdAt"]))
        except (TypeError, ValueError):
            created = datetime.fromtimestamp(entry.stat().st_mtime)
            metadata["createdAt"] = created.isoformat(timespec="seconds")
        metadata["expired"] = created < cutoff
        backups.append(metadata)
    backups.sort(key=lambda item: item.get("createdAt", ""), reverse=True)
    return backups


def get_backup_status():
    settings = get_backup_settings()
    backups = list_backups()
    expired_bytes = sum(int(item.get("sizeBytes", 0) or 0) for item in backups if item.get("expired"))
    total_bytes = sum(int(item.get("sizeBytes", 0) or 0) for item in backups)
    threshold_bytes = int(float(settings["cleanupThresholdGB"]) * 1024 * 1024 * 1024)
    return {
        "settings": settings,
        "totalCount": len(backups),
        "totalBytes": total_bytes,
        "expiredCount": sum(1 for item in backups if item.get("expired")),
        "expiredBytes": expired_bytes,
        "thresholdBytes": threshold_bytes,
        "percent": round(expired_bytes / threshold_bytes * 100, 1) if threshold_bytes else 0,
        "latest": backups[0] if backups else None,
    }


def create_backup(operation):
    settings = get_backup_settings()
    if not settings["triggers"].get(operation, False):
        return None
    with BACKUP_LOCK:
        # 保证数据库已初始化且所有已提交内容已落盘。
        get_conn().close()
        root = backup_root(settings)
        os.makedirs(root, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = os.path.join(root, timestamp)
        suffix = 1
        while os.path.exists(folder):
            folder = os.path.join(root, f"{timestamp}_{suffix:02d}")
            suffix += 1
        os.makedirs(folder)
        snapshot_path = os.path.join(folder, DB_FILE)

        source = sqlite3.connect(DB_PATH)
        target = sqlite3.connect(snapshot_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

        metadata = {
            "id": os.path.basename(folder),
            "createdAt": datetime.now().isoformat(timespec="seconds"),
            "operation": operation,
            "sizeBytes": os.path.getsize(snapshot_path),
        }
        atomic_write_json(os.path.join(folder, BACKUP_META_FILE), metadata)
        return metadata


def resolve_backup_folder(backup_id):
    if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9_]{15,32}", backup_id):
        raise ValueError("备份标识无效")
    root = backup_root()
    folder = os.path.abspath(os.path.join(root, backup_id))
    if os.path.commonpath([root, folder]) != root:
        raise ValueError("备份路径无效")
    if not os.path.isfile(os.path.join(folder, DB_FILE)):
        raise ValueError("备份快照不存在")
    return folder


def restore_backup(backup_id):
    with BACKUP_LOCK:
        folder = resolve_backup_folder(backup_id)
        pre_restore_backup = create_backup("restore")
        source = sqlite3.connect(os.path.join(folder, DB_FILE))
        target = sqlite3.connect(DB_PATH)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
            source.close()
    return {"restored": backup_id, "preRestoreBackup": pre_restore_backup}


def move_to_recycle_bin(path):
    class ShellFileOperation(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR), ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", wintypes.LPVOID),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    operation = ShellFileOperation(None, 3, path + "\0\0", None, 0x40 | 0x10 | 0x04 | 0x400, False, None, None)
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise OSError("无法将过期备份移入 Windows 回收站")


def cleanup_backups(backup_ids):
    if not isinstance(backup_ids, list) or not backup_ids:
        raise ValueError("请选择需要清理的过期备份")
    available = {item["id"]: item for item in list_backups()}
    selected = []
    for backup_id in backup_ids:
        if backup_id not in available or not available[backup_id].get("expired"):
            raise ValueError("只能清理当前清单中已过期的备份")
        selected.append(backup_id)

    with BACKUP_LOCK:
        for backup_id in selected:
            # 经用户确认后移入 Windows 回收站，避免对个人备份目录做不可恢复的直接删除。
            move_to_recycle_bin(resolve_backup_folder(backup_id))
    return {"deleted": selected, "count": len(selected)}


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
    server_version = "FormControlServer/2.0"

    def log_message(self, *args):
        pass

    def send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_text(self, code, text, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
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
        try:
            if path == "/api/health":
                return self.send_json(200, {"ok": True})
            if path == "/api/records":
                return self.send_json(200, load_records())
            if path == "/api/settings":
                return self.send_json(200, load_settings())
            if path == "/api/backups":
                return self.send_json(200, {"items": list_backups(), "status": get_backup_status()})
            if path == "/api/backups/status":
                return self.send_json(200, get_backup_status())
        except Exception as error:
            return self.send_json(500, {"error": str(error)})
        return self.serve_static(path)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}

        try:
            if path == "/api/records":
                # 兼容旧版数组请求，并为新版对象请求提供操作类型。
                records = payload if isinstance(payload, list) else payload.get("records") if isinstance(payload, dict) else None
                operation = payload.get("operation", "edit") if isinstance(payload, dict) else "edit"
                if not isinstance(records, list):
                    return self.send_json(400, {"error": "记录数据格式无效"})
                if operation not in ("create", "edit", "delete", "import", "meta"):
                    return self.send_json(400, {"error": "不支持的写入操作"})
                with BACKUP_LOCK:
                    snapshot = create_backup(operation) if operation in ("edit", "delete", "import") else None
                    save_records(records)
                return self.send_json(200, {"ok": True, "count": len(records), "backup": snapshot})

            if path == "/api/settings":
                if not isinstance(payload, dict):
                    return self.send_json(400, {"error": "设置数据格式无效"})
                save_settings(payload)
                return self.send_json(200, {"ok": True})

            if path == "/api/backups/settings":
                settings = save_backup_settings(payload)
                return self.send_json(200, {"ok": True, "settings": settings, "status": get_backup_status()})

            if path == "/api/backups/restore":
                if not isinstance(payload, dict):
                    return self.send_json(400, {"error": "恢复参数无效"})
                result = restore_backup(payload.get("id"))
                return self.send_json(200, {"ok": True, **result})

            if path == "/api/backups/cleanup":
                if not isinstance(payload, dict):
                    return self.send_json(400, {"error": "清理参数无效"})
                result = cleanup_backups(payload.get("ids"))
                return self.send_json(200, {"ok": True, **result, "status": get_backup_status()})

            if path == "/api/quit":
                self.send_json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
        except ValueError as error:
            return self.send_json(400, {"error": str(error)})
        except Exception as error:
            return self.send_json(500, {"error": str(error)})

        return self.send_json(404, {"error": "not found"})

    def serve_static(self, path):
        if path in ("/", ""):
            path = "/" + INDEX_FILE
        relative = path.lstrip("/")
        full_path = os.path.normpath(os.path.join(APP_DIR, relative))
        if not full_path.startswith(APP_DIR) or not os.path.isfile(full_path):
            return self.send_text(404, "not found")
        if os.path.basename(full_path) == DB_FILE:
            return self.send_text(403, "forbidden")
        content_type = MIME.get(os.path.splitext(full_path)[1].lower(), "application/octet-stream")
        try:
            with open(full_path, "rb") as file:
                data = file.read()
        except OSError:
            return self.send_text(404, "not found")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------------------
# 运行实例与端口
# ---------------------------------------------------------------------------
def create_app_mutex():
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW(None, False, MUTEX_NAME)
    except Exception:
        pass


def shutdown_existing():
    for port in range(DEFAULT_PORT, DEFAULT_PORT + 50):
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/quit",
                data=b"{}",
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=1) as response:
                if response.status == 200:
                    time.sleep(0.5)
                    return
        except Exception:
            continue


def probe_port(port):
    socket_handle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        socket_handle.bind(("127.0.0.1", port))
        socket_handle.close()
        return "free"
    except OSError:
        pass
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
            if response.status == 200:
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
    try:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    except Exception:
        pass


def main():
    if "--shutdown" in sys.argv:
        shutdown_existing()
        return
    create_app_mutex()
    status, port = find_port(DEFAULT_PORT)
    if port is None:
        sys.stderr.write("无法找到可用端口，启动失败。\n")
        return
    if status == "reuse":
        open_browser(port)
        return
    get_conn().close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Timer(0.6, open_browser, args=(port,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
