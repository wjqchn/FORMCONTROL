#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
质量文控平台 2.0 · 本地数据服务

职责：
  - 在同一 origin (http://127.0.0.1:PORT) 下托管安装目录中的 HTML 文件
  - 提供 /api/* 接口，把业务数据落盘到安装路径下的 data.sqlite
  - 提供可配置的本地数据库快照、查询、恢复和用户确认后的过期清理
  - 提供账户管理、角色权限管理与会话认证
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
import hashlib
import secrets
from datetime import datetime, timedelta
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

INDEX_FILE = "表格分类汇总.html"
DB_FILE = "data.sqlite"
AUTH_FILE = "auth.sqlite"
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
# 认证与权限常量
# ---------------------------------------------------------------------------
SESSIONS = {}                  # token -> session dict
SESSION_TIMEOUT = 8 * 3600    # 8 小时
ALL_PERMISSIONS = ["import", "entry", "edit", "export", "backup_settings", "backup_query"]
PERMISSION_LABELS = {
    "import": "批量导入",
    "entry": "录入",
    "edit": "编辑",
    "export": "导出",
    "backup_settings": "备份设置",
    "backup_query": "备份查询",
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
# 静态资源目录：打包成 exe（PyInstaller --onefile）后，HTML 等被解压到 sys._MEIPASS，
# 需从那里读取；开发态则直接用应用目录本身。
STATIC_DIR = sys._MEIPASS if getattr(sys, "frozen", False) else APP_DIR
DB_PATH = os.path.join(APP_DIR, DB_FILE)
AUTH_PATH = os.path.join(APP_DIR, AUTH_FILE)
BACKUP_SETTINGS_PATH = os.path.join(APP_DIR, BACKUP_SETTINGS_FILE)

# 账户库（auth.sqlite）专用备份目录，独立于业务数据备份，且不会被业务还原卷走。
AUTH_BACKUP_ROOT = os.path.join(APP_DIR, "auth_backups")
AUTH_BACKUP_META = "auth_backup.json"
AUTH_BACKUP_RETENTION = 20          # 自动/手动账户备份最多保留份数
AUTH_BACKUP_MARKER = os.path.join(AUTH_BACKUP_ROOT, ".keep")


def get_conn():
    """业务数据库连接：仅承载 records / settings。账户与权限另存于 auth.sqlite，
    备份与还原只针对本库，因此还原业务数据不会影响账户系统。"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, data TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn


def get_auth_conn():
    """账户数据库连接：承载 roles / users（账户与权限的系统配置）。"""
    conn = sqlite3.connect(AUTH_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS roles (
        id TEXT PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        permissions TEXT NOT NULL DEFAULT '[]',
        is_admin INTEGER NOT NULL DEFAULT 0,
        is_system INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role_id TEXT NOT NULL,
        email TEXT DEFAULT '',
        department TEXT DEFAULT '',
        is_system INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        FOREIGN KEY (role_id) REFERENCES roles(id)
    )""")
    _ensure_auth_schema_columns(conn)
    conn.commit()
    return conn


def _ensure_auth_schema_columns(conn):
    """为当前连接的 auth.sqlite 补齐兼容列。

    每个数据库文件都独立检查：启动迁移可能先连接临时/旧账户库，
    因此不能用进程级标志跳过后续新建账户库的表结构补齐。
    """
    for table, column, definition in (
        ("roles", "is_system", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "is_system", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "is_active", "INTEGER NOT NULL DEFAULT 1"),
        ("users", "must_change_pw", "INTEGER NOT NULL DEFAULT 0"),
    ):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
# 密码哈希
# ---------------------------------------------------------------------------
def hash_password(password):
    salt = secrets.token_hex(16)
    hash_value = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 100000)
    return f"{salt}:{hash_value.hex()}"


def password_strength(password):
    """返回密码强度；首次管理员初始化要求至少 8 位且四类字符至少三类。"""
    categories = sum((
        any("A" <= char <= "Z" for char in password),
        any("a" <= char <= "z" for char in password),
        any("0" <= char <= "9" for char in password),
        any(char in "!@#$%^&*()_+-=[]{}|;:,.~" for char in password),
    ))
    missing = 4 - categories
    if len(password) < 8 or missing >= 3:
        return "low", categories
    if missing == 2:
        return "medium", categories
    if missing == 1:
        return "high", categories
    return "safe", categories


def verify_password(password, stored):
    if not stored or ":" not in stored:
        return False
    salt, hash_hex = stored.split(":", 1)
    try:
        hash_value = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 100000)
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(hash_value.hex(), hash_hex)


# ---------------------------------------------------------------------------
# 用户与角色 CRUD
# ---------------------------------------------------------------------------
def get_role(conn, role_id):
    row = conn.execute("SELECT id, name, permissions, is_admin, is_system, created_at FROM roles WHERE id = ?", (role_id,)).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "permissions": json.loads(row[2]) if row[2] else [],
        "is_admin": bool(row[3]),
        "is_system": bool(row[4]),
        "created_at": row[5],
    }


def get_role_by_name(conn, name):
    row = conn.execute("SELECT id FROM roles WHERE name = ?", (name,)).fetchone()
    if not row:
        return None
    return get_role(conn, row[0])


def list_roles():
    conn = get_auth_conn()
    try:
        rows = conn.execute("SELECT id FROM roles ORDER BY created_at").fetchall()
        return [get_role(conn, r[0]) for r in rows]
    finally:
        conn.close()


def save_role(payload):
    if not isinstance(payload, dict):
        raise ValueError("角色数据格式无效")
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("请输入角色名称")
    permissions = payload.get("permissions", [])
    if not isinstance(permissions, list):
        raise ValueError("权限格式无效")
    permissions = [p for p in permissions if p in ALL_PERMISSIONS]
    role_id = str(payload.get("id", "")).strip()

    conn = get_auth_conn()
    try:
        if role_id:
            existing = get_role(conn, role_id)
            if not existing:
                raise ValueError("角色不存在")
            if existing["is_system"]:
                is_admin = 1  # 系统管理员角色必须保留管理端访问权限
            else:
                is_admin = 1 if payload.get("is_admin") else 0
            if existing["name"] != name:
                dup = get_role_by_name(conn, name)
                if dup:
                    raise ValueError("角色名称已存在")
            conn.execute(
                "UPDATE roles SET name = ?, permissions = ?, is_admin = ? WHERE id = ?",
                (name, json.dumps(permissions, ensure_ascii=False), is_admin, role_id),
            )
        else:
            if get_role_by_name(conn, name):
                raise ValueError("角色名称已存在")
            role_id = secrets.token_hex(8)
            is_admin = 1 if payload.get("is_admin") else 0
            conn.execute(
                "INSERT INTO roles (id, name, permissions, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
                (role_id, name, json.dumps(permissions, ensure_ascii=False), is_admin, datetime.now().isoformat(timespec="seconds")),
            )
        conn.commit()
        create_auth_backup("auto")
        return get_role(conn, role_id)
    finally:
        conn.close()


def delete_role(role_id):
    conn = get_auth_conn()
    try:
        role = get_role(conn, role_id)
        if not role:
            raise ValueError("角色不存在")
        if role["is_system"]:
            raise ValueError("默认管理员角色不可删除")
        user_count = conn.execute("SELECT COUNT(*) FROM users WHERE role_id = ?", (role_id,)).fetchone()[0]
        if user_count > 0:
            raise ValueError(f"该角色下还有 {user_count} 个用户，无法删除")
        if role and role["is_admin"]:
            admin_count = conn.execute("SELECT COUNT(*) FROM roles WHERE is_admin = 1").fetchone()[0]
            if admin_count <= 1:
                raise ValueError("系统至少需要保留一个管理员角色")
        conn.execute("DELETE FROM roles WHERE id = ?", (role_id,))
        conn.commit()
        create_auth_backup("auto")
        return {"deleted": role_id}
    finally:
        conn.close()


def get_user(conn, user_id):
    row = conn.execute("SELECT id, username, password_hash, role_id, email, department, is_system, is_active, must_change_pw, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "username": row[1],
        "password_hash": row[2],
        "role_id": row[3],
        "email": row[4] or "",
        "department": row[5] or "",
        "is_system": bool(row[6]),
        "is_active": bool(row[7]),
        "must_change_pw": bool(row[8]),
        "created_at": row[9],
    }


def get_user_by_username(conn, username):
    row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        return None
    return get_user(conn, row[0])


def user_with_role(conn, user):
    """返回不含 password_hash 的用户信息，附带角色名和权限。"""
    if not user:
        return None
    role = get_role(conn, user["role_id"])
    return {
        "id": user["id"],
        "username": user["username"],
        "role_id": user["role_id"],
        "role_name": role["name"] if role else "",
        "email": user["email"],
        "department": user["department"],
        "permissions": role["permissions"] if role else [],
        "is_admin": role["is_admin"] if role else False,
        "is_system": user["is_system"] if user else False,
        "is_active": user["is_active"] if user else False,
        "must_change_pw": user.get("must_change_pw", False) if user else False,
        "created_at": user["created_at"],
    }


def list_users():
    conn = get_auth_conn()
    try:
        rows = conn.execute("SELECT id FROM users ORDER BY created_at").fetchall()
        return [user_with_role(conn, get_user(conn, r[0])) for r in rows]
    finally:
        conn.close()


def save_user(payload):
    if not isinstance(payload, dict):
        raise ValueError("用户数据格式无效")
    username = str(payload.get("username", "")).strip()
    if not username:
        raise ValueError("请输入用户名")
    role_id = str(payload.get("role_id", "")).strip()
    if not role_id:
        raise ValueError("请选择角色")
    email = str(payload.get("email", "")).strip()
    department = str(payload.get("department", "")).strip()
    user_id = str(payload.get("id", "")).strip()
    password = str(payload.get("password", "")).strip()

    conn = get_auth_conn()
    try:
        if not get_role(conn, role_id):
            raise ValueError("所选角色不存在")
        if user_id:
            existing = get_user(conn, user_id)
            if not existing:
                raise ValueError("用户不存在")
            if not existing.get("is_active", True):
                raise ValueError("失活账户不允许编辑")
            if existing["username"] != username:
                if existing["is_system"]:
                    raise ValueError("默认管理员账户的用户名不可修改")
                if get_user_by_username(conn, username):
                    raise ValueError("用户名已存在")
            if existing["is_system"] and role_id != existing["role_id"]:
                raise ValueError("默认管理员账户的角色不可修改")
            if password:
                strength, _ = password_strength(password)
                if strength in ("low", "medium"):
                    raise ValueError("密码至少 8 位，并需包含大写字母、小写字母、数字、特殊符号中的至少三类")
                password_hash = hash_password(password)
                must_change = 1
            else:
                password_hash = existing["password_hash"]
                must_change = existing.get("must_change_pw", 0)
            conn.execute(
                "UPDATE users SET username = ?, password_hash = ?, role_id = ?, email = ?, department = ?, must_change_pw = ? WHERE id = ?",
                (username, password_hash, role_id, email, department, must_change, user_id),
            )
        else:
            if not password:
                raise ValueError("请输入密码")
            strength, _ = password_strength(password)
            if strength in ("low", "medium"):
                raise ValueError("密码至少 8 位，并需包含大写字母、小写字母、数字、特殊符号中的至少三类")
            if get_user_by_username(conn, username):
                raise ValueError("用户名已存在")
            user_id = secrets.token_hex(8)
            password_hash = hash_password(password)
            conn.execute(
                "INSERT INTO users (id, username, password_hash, role_id, email, department, is_system, is_active, must_change_pw, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, username, password_hash, role_id, email, department, 0, 1, 1, datetime.now().isoformat(timespec="seconds")),
            )
        conn.commit()
        create_auth_backup("auto")
        return user_with_role(conn, get_user(conn, user_id))
    finally:
        conn.close()


def deactivate_user(user_id, current_user_id):
    """将用户设为失活：禁用其登录与编辑，但不删除账户（账户不允许物理删除）。"""
    conn = get_auth_conn()
    try:
        user = get_user(conn, user_id)
        if not user:
            raise ValueError("用户不存在")
        if user["is_system"]:
            raise ValueError("默认管理员账户不可失活")
        if user_id == current_user_id:
            raise ValueError("不能失活当前登录用户")
        if not user.get("is_active", True):
            raise ValueError("该账户已处于失活状态")
        role = get_role(conn, user["role_id"])
        if role and role["is_admin"]:
            active_admin_count = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role_id IN (SELECT id FROM roles WHERE is_admin = 1) AND is_active = 1"
            ).fetchone()[0]
            if active_admin_count <= 1:
                raise ValueError("系统至少需要保留一个活跃管理员用户")
        conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
        conn.commit()
        create_auth_backup("auto")
        return {"deactivated": user_id}
    finally:
        conn.close()


def activate_user(user_id):
    """将失活用户重新激活：恢复其登录与编辑能力（账户不允许物理删除）。"""
    conn = get_auth_conn()
    try:
        user = get_user(conn, user_id)
        if not user:
            raise ValueError("用户不存在")
        if user.get("is_active", True):
            raise ValueError("该账户已处于活跃状态")
        conn.execute("UPDATE users SET is_active = 1 WHERE id = ?", (user_id,))
        conn.commit()
        create_auth_backup("auto")
        return {"activated": user_id}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 会话管理
# ---------------------------------------------------------------------------
def create_session(user, role):
    token = secrets.token_hex(32)
    SESSIONS[token] = {
        "user_id": user["id"],
        "username": user["username"],
        "role_id": user["role_id"],
        "permissions": role["permissions"] if role else [],
        "is_admin": bool(role["is_admin"]) if role else False,
        "is_system": bool(user.get("is_system")),
        "expires": time.time() + SESSION_TIMEOUT,
    }
    return token


def get_session(token):
    if not token:
        return None
    session = SESSIONS.get(token)
    if not session:
        return None
    if time.time() > session["expires"]:
        SESSIONS.pop(token, None)
        return None
    session["expires"] = time.time() + SESSION_TIMEOUT
    return session


def destroy_session(token):
    SESSIONS.pop(token, None)


def migrate_legacy_auth():
    """从旧版 data.sqlite（账户曾与业务数据同库）迁移 roles/users 到 auth.sqlite。
    - 迁移仅在 auth 库为空时执行一次（幂等）。
    - 只要 auth 已拥有账户数据，就清理业务库遗留的 roles/users 孤儿表，
      确保整库备份不再包含账户、账户彻底只存在于 auth.sqlite。"""
    auth_conn = get_auth_conn()
    try:
        if not os.path.isfile(DB_PATH):
            return
        legacy = sqlite3.connect(DB_PATH)
        try:
            legacy_tables = {r[0] for r in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "roles" not in legacy_tables or "users" not in legacy_tables:
                return  # 业务库已无账户表，无需处理
            # 1) 迁移：仅当 auth 为空时复制
            if auth_conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0] == 0:
                for row in legacy.execute("SELECT id, name, permissions, is_admin, is_system, created_at FROM roles").fetchall():
                    auth_conn.execute(
                        "INSERT OR IGNORE INTO roles (id, name, permissions, is_admin, is_system, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (row[0], row[1], row[2], row[3], row[4], row[5]),
                    )
                for row in legacy.execute("SELECT id, username, password_hash, role_id, email, department, is_system, created_at FROM users").fetchall():
                    auth_conn.execute(
                        "INSERT OR IGNORE INTO users (id, username, password_hash, role_id, email, department, is_system, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7]),
                    )
                auth_conn.commit()
            # 2) 只要 auth 已有账户数据，就清理业务库孤儿表
            if auth_conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0] > 0:
                legacy.execute("DROP TABLE IF EXISTS roles")
                legacy.execute("DROP TABLE IF EXISTS users")
                legacy.commit()
        finally:
            legacy.close()
    finally:
        auth_conn.close()


def init_default_auth():
    """确保账户库存在默认管理员角色与默认管理员账户（账户与权限独立存于 auth.sqlite）。"""
    migrate_legacy_auth()
    conn = get_auth_conn()
    try:
        role_count = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
        if role_count == 0:
            admin_role_id = secrets.token_hex(8)
            conn.execute(
                "INSERT INTO roles (id, name, permissions, is_admin, is_system, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (admin_role_id, "系统管理员", json.dumps(ALL_PERMISSIONS), 1, 1, datetime.now().isoformat(timespec="seconds")),
            )
            operator_role_id = secrets.token_hex(8)
            conn.execute(
                "INSERT INTO roles (id, name, permissions, is_admin, is_system, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (operator_role_id, "操作员", json.dumps(["entry", "edit", "export"]), 0, 0, datetime.now().isoformat(timespec="seconds")),
            )
            viewer_role_id = secrets.token_hex(8)
            conn.execute(
                "INSERT INTO roles (id, name, permissions, is_admin, is_system, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (viewer_role_id, "查看员", json.dumps(["export"]), 0, 0, datetime.now().isoformat(timespec="seconds")),
            )

        # 确保至少有一个管理员用户，避免账户库被清空后无法登录
        admin_user = conn.execute(
            "SELECT id FROM users WHERE role_id IN (SELECT id FROM roles WHERE is_admin = 1) LIMIT 1"
        ).fetchone()
        if not admin_user:
            admin_role = conn.execute("SELECT id FROM roles WHERE is_admin = 1 LIMIT 1").fetchone()
            if admin_role:
                conn.execute(
                    "INSERT INTO users (id, username, password_hash, role_id, email, department, is_system, must_change_pw, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (secrets.token_hex(8), "admin", hash_password("admin123"), admin_role[0], "", "系统管理部", 1, 1, datetime.now().isoformat(timespec="seconds")),
                )

        # 确保默认管理员账户与默认管理员角色始终被标记为系统保护
        conn.execute("UPDATE users SET is_system = 1 WHERE username = 'admin'")
        conn.execute("UPDATE roles SET is_system = 1 WHERE name = '系统管理员'")
        # 默认管理员若仍在使用初始弱口令 admin123，强制其首次登录修改密码
        _admin_row = conn.execute("SELECT id, password_hash FROM users WHERE username = 'admin'").fetchone()
        if _admin_row and verify_password("admin123", _admin_row[1]):
            conn.execute("UPDATE users SET must_change_pw = 1 WHERE id = ?", (_admin_row[0],))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 数据库备份模块
# ---------------------------------------------------------------------------
def _replace_with_retry(temp_path, target):
    """Windows 上目标文件常因杀毒/索引瞬间加锁导致 os.replace 失败，
    这里先尝试删除目标再 rename，并做指数退避重试。"""
    last_err = None
    for attempt in range(12):
        try:
            if os.path.exists(target):
                try:
                    os.remove(target)
                except OSError:
                    pass
            os.rename(temp_path, target)
            return
        except OSError as exc:
            last_err = exc
            time.sleep(0.08 * (attempt + 1))
    try:
        os.replace(temp_path, target)
    except OSError:
        if last_err:
            raise last_err
        raise


def atomic_write_json(path, value):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tmp_", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
        _replace_with_retry(temp_path, path)
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
            # 仅恢复业务表（records / settings）。账户与权限存于独立的 auth.sqlite，
            # 不参与业务数据还原，因此还原快照永远不会卷走账户系统。
            for table in ("records", "settings"):
                cols = [r[1] for r in source.execute(f"PRAGMA table_info({table})").fetchall()]
                if not cols:
                    continue
                col_names = ",".join(f'"{c}"' for c in cols)
                placeholders = ",".join("?" for _ in cols)
                target.execute(f"DELETE FROM {table}")
                rows = source.execute(f"SELECT {col_names} FROM {table}").fetchall()
                if rows:
                    target.executemany(
                        f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})",
                        rows,
                    )
            target.commit()
        finally:
            target.close()
            source.close()
    return {"restored": backup_id, "preRestoreBackup": pre_restore_backup}


# ---------------------------------------------------------------------------
# 账户库（auth.sqlite）备份与还原 —— 独立于业务数据，仅默认管理员账户可操作
# ---------------------------------------------------------------------------
def get_auth_status():
    """返回当前账户库概览，用于管理端展示与备份元数据。"""
    if not os.path.isfile(AUTH_PATH):
        return {"exists": False, "userCount": 0, "roleCount": 0, "adminRoleCount": 0,
                "systemAdminExists": False, "mtime": None}
    conn = sqlite3.connect(AUTH_PATH)
    try:
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        role_count = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
        admin_role_count = conn.execute("SELECT COUNT(*) FROM roles WHERE is_admin = 1").fetchone()[0]
        system_admin = conn.execute("SELECT COUNT(*) FROM users WHERE is_system = 1 AND username = 'admin'").fetchone()[0]
        mtime = os.path.getmtime(AUTH_PATH)
    finally:
        conn.close()
    return {
        "exists": True,
        "userCount": user_count,
        "roleCount": role_count,
        "adminRoleCount": admin_role_count,
        "systemAdminExists": bool(system_admin),
        "mtime": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
    }


def _auth_snapshot_meta(folder, label):
    snapshot_path = os.path.join(folder, AUTH_FILE)
    status = get_auth_status()
    return {
        "id": os.path.basename(folder),
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "sizeBytes": os.path.getsize(snapshot_path) if os.path.isfile(snapshot_path) else 0,
        "userCount": status["userCount"],
        "roleCount": status["roleCount"],
    }


def prune_auth_backups():
    """保留最近 AUTH_BACKUP_RETENTION 份，超出部分直接删除（自动清理，非用户操作）。"""
    if not os.path.isdir(AUTH_BACKUP_ROOT):
        return
    folders = [
        entry.path for entry in os.scandir(AUTH_BACKUP_ROOT)
        if entry.is_dir(follow_symlinks=False) and os.path.isfile(os.path.join(entry.path, AUTH_FILE))
    ]
    folders.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for old in folders[AUTH_BACKUP_RETENTION:]:
        shutil.rmtree(old, ignore_errors=True)


def create_auth_backup(label="manual"):
    """复制 auth.sqlite 到一个带时间戳的快照目录。返回元数据或 None（源库不存在时）。"""
    if not os.path.isfile(AUTH_PATH):
        return None
    os.makedirs(AUTH_BACKUP_ROOT, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = os.path.join(AUTH_BACKUP_ROOT, timestamp)
    suffix = 1
    while os.path.exists(folder):
        folder = os.path.join(AUTH_BACKUP_ROOT, f"{timestamp}_{suffix:02d}")
        suffix += 1
    os.makedirs(folder)
    snapshot_path = os.path.join(folder, AUTH_FILE)
    source = sqlite3.connect(AUTH_PATH)
    target = sqlite3.connect(snapshot_path)
    source.execute("PRAGMA busy_timeout=10000")
    target.execute("PRAGMA busy_timeout=10000")
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    metadata = _auth_snapshot_meta(folder, label)
    atomic_write_json(os.path.join(folder, AUTH_BACKUP_META), metadata)
    prune_auth_backups()
    return metadata


def list_auth_backups():
    if not os.path.isdir(AUTH_BACKUP_ROOT):
        return []
    backups = []
    for entry in os.scandir(AUTH_BACKUP_ROOT):
        if not entry.is_dir(follow_symlinks=False):
            continue
        meta_path = os.path.join(entry.path, AUTH_BACKUP_META)
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path, "r", encoding="utf-8") as file:
                metadata = json.load(file)
        except (OSError, ValueError, TypeError):
            continue
        metadata.setdefault("id", os.path.basename(entry.path))
        metadata.setdefault("createdAt", datetime.fromtimestamp(entry.stat().st_mtime).isoformat(timespec="seconds"))
        metadata.setdefault("label", "manual")
        metadata.setdefault("sizeBytes", os.path.getsize(os.path.join(entry.path, AUTH_FILE)) if os.path.isfile(os.path.join(entry.path, AUTH_FILE)) else 0)
        backups.append(metadata)
    backups.sort(key=lambda item: str(item.get("createdAt", "")), reverse=True)
    return backups


def restore_auth_backup(backup_id):
    """用指定快照覆盖当前 auth.sqlite，并清空所有登录会话（强制重新登录）。"""
    if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9_]{15,40}", backup_id):
        raise ValueError("账户备份标识无效")
    root = os.path.abspath(AUTH_BACKUP_ROOT)
    folder = os.path.abspath(os.path.join(root, backup_id))
    if os.path.commonpath([root, folder]) != root:
        raise ValueError("账户备份路径无效")
    snapshot = os.path.join(folder, AUTH_FILE)
    if not os.path.isfile(snapshot):
        raise ValueError("账户备份快照不存在")
    source = sqlite3.connect(snapshot)
    target = sqlite3.connect(AUTH_PATH)
    source.execute("PRAGMA busy_timeout=10000")
    target.execute("PRAGMA busy_timeout=10000")
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    SESSIONS.clear()  # 账户已回滚，强制所有在线用户重新登录
    return {"restored": backup_id, **get_auth_status()}


def delete_auth_backup(backup_id):
    """删除一份账户备份（移入回收站，便于误删恢复）。"""
    if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9_]{15,40}", backup_id):
        raise ValueError("账户备份标识无效")
    root = os.path.abspath(AUTH_BACKUP_ROOT)
    folder = os.path.abspath(os.path.join(root, backup_id))
    if os.path.commonpath([root, folder]) != root:
        raise ValueError("账户备份路径无效")
    if not os.path.isdir(folder):
        raise ValueError("账户备份不存在")
    move_to_recycle_bin(folder)
    return {"deleted": backup_id}


def move_to_recycle_bin(path):
    """优先移入 Windows 回收站；后台服务/无交互桌面环境下回收站不可用时，
    优雅降级为直接删除，确保删除操作始终可用。"""
    try:
        class ShellFileOperation(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", wintypes.WORD),
                ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", wintypes.LPVOID),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        operation = ShellFileOperation(None, 3, path + "\0\0", None, 0x40 | 0x10 | 0x04 | 0x400, False, None, None)
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
        if result == 0 and not operation.fAnyOperationsAborted:
            return
    except Exception:
        pass
    # 降级：直接删除
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    # ---- 认证辅助 ----
    def get_bearer_token(self):
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip()
        return ""

    def get_current_session(self):
        return get_session(self.get_bearer_token())

    def require_auth(self):
        """返回 session dict 或发送 401 并返回 None。"""
        session = self.get_current_session()
        if not session:
            self.send_json(401, {"error": "未登录或会话已过期"})
            return None
        return session

    def require_permission(self, perm):
        """返回 session 或发送 403 并返回 None。"""
        session = self.require_auth()
        if not session:
            return None
        if session.get("is_admin") or perm in session.get("permissions", []):
            return session
        self.send_json(403, {"error": "没有操作权限"})
        return None

    def require_admin(self):
        """返回 session 或发送 403 并返回 None。"""
        session = self.require_auth()
        if not session:
            return None
        if session.get("is_admin"):
            return session
        self.send_json(403, {"error": "需要管理员权限"})
        return None

    def require_super_admin(self):
        """仅默认管理员账户（is_system）可操作账户库备份/还原。"""
        session = self.require_auth()
        if not session:
            return None
        if session.get("is_system"):
            return session
        self.send_json(403, {"error": "仅默认管理员账户可操作账户备份与还原"})
        return None

    # ---- GET ----
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/health":
                return self.send_json(200, {"ok": True})

            if path == "/api/auth/bootstrap":
                # 双重保障：少数旧安装/服务复用场景可能留下空 auth.sqlite。
                # 登录页首次读取状态时主动补建系统角色与默认管理员，确保不会落入空白登录表单。
                conn = get_auth_conn()
                try:
                    has_admin = bool(get_user_by_username(conn, "admin"))
                finally:
                    conn.close()
                if not has_admin:
                    init_default_auth()
                # 首次部署时，默认管理员尚未设置自己的密码：前端无需保留登录令牌，
                # 直接展示“设置管理员密码”引导。接口不返回账户或密码信息。
                conn = get_auth_conn()
                try:
                    admin = get_user_by_username(conn, "admin")
                    required = bool(admin and admin.get("is_system") and admin.get("is_active", True)
                                    and admin.get("must_change_pw")
                                    and verify_password("admin123", admin["password_hash"]))
                finally:
                    conn.close()
                return self.send_json(200, {"initial_admin_password_required": required})

            if path == "/api/auth/session":
                session = self.require_auth()
                if not session:
                    return
                conn = get_auth_conn()
                try:
                    user = get_user(conn, session["user_id"])
                    info = user_with_role(conn, user) if user else None
                finally:
                    conn.close()
                if not info:
                    destroy_session(self.get_bearer_token())
                    return self.send_json(401, {"error": "用户不存在"})
                return self.send_json(200, {"user": info})

            if path == "/api/records":
                if not self.require_auth():
                    return
                return self.send_json(200, load_records())

            if path == "/api/settings":
                if not self.require_auth():
                    return
                return self.send_json(200, load_settings())

            if path == "/api/backups":
                if not self.require_permission("backup_query"):
                    return
                return self.send_json(200, {"items": list_backups(), "status": get_backup_status()})

            if path == "/api/backups/status":
                if not self.require_auth():
                    return
                return self.send_json(200, get_backup_status())

            if path == "/api/admin/users":
                if not self.require_admin():
                    return
                return self.send_json(200, {"users": list_users()})

            if path == "/api/admin/roles":
                if not self.require_admin():
                    return
                return self.send_json(200, {"roles": list_roles(), "permissions": ALL_PERMISSIONS, "permissionLabels": PERMISSION_LABELS})

            if path == "/api/auth-backups":
                if not self.require_super_admin():
                    return
                return self.send_json(200, {"items": list_auth_backups(), "status": get_auth_status()})

        except Exception as error:
            return self.send_json(500, {"error": str(error)})
        return self.serve_static(path)

    # ---- POST ----
    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}

        try:
            # ---- 认证接口 ----
            if path == "/api/auth/login":
                username = str(payload.get("username", "")).strip()
                password = str(payload.get("password", "")).strip()
                if not username or not password:
                    return self.send_json(400, {"error": "请输入用户名和密码"})
                conn = get_auth_conn()
                try:
                    user = get_user_by_username(conn, username)
                    if not user or not verify_password(password, user["password_hash"]):
                        return self.send_json(401, {"error": "用户名或密码错误"})
                    if not user.get("is_active", True):
                        return self.send_json(403, {"error": "该账户已失活，无法登录，请联系管理员"})
                    role = get_role(conn, user["role_id"])
                    if not role:
                        return self.send_json(403, {"error": "用户角色不存在，请联系管理员"})
                    # 首次部署的默认管理员不创建持久登录会话；改密接口会通过
                    # 一次性设置流程单独核验 admin123，避免关闭网页后残留会话。
                    if user.get("must_change_pw") and user.get("is_system") and username == "admin" and verify_password("admin123", user["password_hash"]):
                        return self.send_json(200, {"setup_required": True, "user": {"username": "admin", "must_change_pw": True}})
                    token = create_session(user, role)
                    info = user_with_role(conn, user)
                finally:
                    conn.close()
                return self.send_json(200, {"token": token, "user": info})

            if path == "/api/auth/logout":
                destroy_session(self.get_bearer_token())
                return self.send_json(200, {"ok": True})

            if path == "/api/auth/setup-admin-password":
                # 首次部署的默认管理员初始化：页面固定展示 admin，只需设置并确认新密码。
                # 在初始化完成前不创建会话；成功后才发放正式登录令牌。
                new_password = str(payload.get("new_password", "")).strip()
                if not new_password:
                    return self.send_json(400, {"error": "请输入管理员新密码"})
                strength, _ = password_strength(new_password)
                if strength in ("low", "medium"):
                    return self.send_json(400, {"error": "密码至少 8 位，并需包含大写字母、小写字母、数字、特殊符号中的至少三类"})
                conn = get_auth_conn()
                try:
                    user = get_user_by_username(conn, "admin")
                    if not user or not user.get("is_system") or not user.get("is_active", True):
                        return self.send_json(403, {"error": "默认管理员账户不可用，请联系系统维护人员"})
                    if not user.get("must_change_pw") or not verify_password("admin123", user["password_hash"]):
                        return self.send_json(409, {"error": "管理员密码已设置，请使用登录入口"})
                    role = get_role(conn, user["role_id"])
                    if not role:
                        return self.send_json(403, {"error": "用户角色不存在，请联系系统维护人员"})
                    conn.execute(
                        "UPDATE users SET password_hash = ?, must_change_pw = 0 WHERE id = ?",
                        (hash_password(new_password), user["id"]),
                    )
                    conn.commit()
                    create_auth_backup("auto")
                    updated_user = get_user(conn, user["id"])
                    token = create_session(updated_user, role)
                    info = user_with_role(conn, updated_user)
                finally:
                    conn.close()
                return self.send_json(200, {"ok": True, "token": token, "user": info})

            if path == "/api/auth/change-password":
                session = self.require_auth()
                if not session:
                    return
                old_password = str(payload.get("old_password", "")).strip()
                new_password = str(payload.get("new_password", "")).strip()
                if not old_password or not new_password:
                    return self.send_json(400, {"error": "请输入原密码和新密码"})
                strength, _ = password_strength(new_password)
                if strength in ("low", "medium"):
                    return self.send_json(400, {"error": "密码至少 8 位，并需包含大写字母、小写字母、数字、特殊符号中的至少三类"})
                conn = get_auth_conn()
                try:
                    user = get_user(conn, session["user_id"])
                    if not user:
                        return self.send_json(404, {"error": "用户不存在"})
                    if not verify_password(old_password, user["password_hash"]):
                        return self.send_json(400, {"error": "原密码不正确"})
                    conn.execute(
                        "UPDATE users SET password_hash = ?, must_change_pw = 0 WHERE id = ?",
                        (hash_password(new_password), user["id"]),
                    )
                    conn.commit()
                    create_auth_backup("auto")
                    info = user_with_role(conn, get_user(conn, user["id"]))
                finally:
                    conn.close()
                return self.send_json(200, {"ok": True, "user": info})

            # ---- 记录接口 ----
            if path == "/api/records":
                records = payload if isinstance(payload, list) else payload.get("records") if isinstance(payload, dict) else None
                operation = payload.get("operation", "edit") if isinstance(payload, dict) else "edit"
                if not isinstance(records, list):
                    return self.send_json(400, {"error": "记录数据格式无效"})
                if operation not in ("create", "edit", "delete", "import", "meta"):
                    return self.send_json(400, {"error": "不支持的写入操作"})
                # 权限映射：create -> entry, edit/delete -> edit, import -> import, meta -> 任意登录
                if operation == "meta":
                    if not self.require_auth():
                        return
                elif operation == "create":
                    if not self.require_permission("entry"):
                        return
                elif operation == "import":
                    if not self.require_permission("import"):
                        return
                else:
                    if not self.require_permission("edit"):
                        return
                with BACKUP_LOCK:
                    snapshot = create_backup(operation) if operation in ("edit", "delete", "import") else None
                    save_records(records)
                return self.send_json(200, {"ok": True, "count": len(records), "backup": snapshot})

            # ---- 设置接口 ----
            if path == "/api/settings":
                if not self.require_auth():
                    return
                if not isinstance(payload, dict):
                    return self.send_json(400, {"error": "设置数据格式无效"})
                save_settings(payload)
                return self.send_json(200, {"ok": True})

            # ---- 备份接口 ----
            if path == "/api/backups/settings":
                if not self.require_permission("backup_settings"):
                    return
                settings = save_backup_settings(payload)
                return self.send_json(200, {"ok": True, "settings": settings, "status": get_backup_status()})

            if path == "/api/backups/restore":
                if not self.require_permission("backup_query"):
                    return
                if not isinstance(payload, dict):
                    return self.send_json(400, {"error": "恢复参数无效"})
                result = restore_backup(payload.get("id"))
                return self.send_json(200, {"ok": True, **result})

            if path == "/api/backups/cleanup":
                if not self.require_permission("backup_query"):
                    return
                if not isinstance(payload, dict):
                    return self.send_json(400, {"error": "清理参数无效"})
                result = cleanup_backups(payload.get("ids"))
                return self.send_json(200, {"ok": True, **result, "status": get_backup_status()})

            # ---- 管理员：用户管理 ----
            if path == "/api/admin/users":
                if not self.require_admin():
                    return
                result = save_user(payload)
                return self.send_json(200, {"ok": True, "user": result})

            if path == "/api/admin/users/deactivate":
                if not self.require_admin():
                    return
                session = self.get_current_session()
                target_id = str(payload.get("id", "")).strip()
                if not target_id:
                    return self.send_json(400, {"error": "请指定要失活的用户"})
                result = deactivate_user(target_id, session["user_id"] if session else "")
                return self.send_json(200, {"ok": True, **result, "users": list_users()})

            if path == "/api/admin/users/activate":
                if not self.require_admin():
                    return
                target_id = str(payload.get("id", "")).strip()
                if not target_id:
                    return self.send_json(400, {"error": "请指定要激活的用户"})
                result = activate_user(target_id)
                return self.send_json(200, {"ok": True, **result, "users": list_users()})

            # ---- 管理员：角色管理 ----
            if path == "/api/admin/roles":
                if not self.require_admin():
                    return
                result = save_role(payload)
                return self.send_json(200, {"ok": True, "role": result})

            if path == "/api/admin/roles/delete":
                if not self.require_admin():
                    return
                target_id = str(payload.get("id", "")).strip()
                if not target_id:
                    return self.send_json(400, {"error": "请指定要删除的角色"})
                result = delete_role(target_id)
                return self.send_json(200, {"ok": True, **result, "roles": list_roles()})

            # ---- 账户库备份（仅默认管理员账户可操作） ----
            if path == "/api/auth-backups":
                if not self.require_super_admin():
                    return
                metadata = create_auth_backup("manual")
                return self.send_json(200, {"ok": True, "backup": metadata, "items": list_auth_backups(), "status": get_auth_status()})

            if path == "/api/auth-backups/restore":
                if not self.require_super_admin():
                    return
                if not isinstance(payload, dict):
                    return self.send_json(400, {"error": "参数无效"})
                result = restore_auth_backup(payload.get("id"))
                return self.send_json(200, {"ok": True, **result})

            if path == "/api/auth-backups/delete":
                if not self.require_super_admin():
                    return
                if not isinstance(payload, dict):
                    return self.send_json(400, {"error": "参数无效"})
                result = delete_auth_backup(payload.get("id"))
                return self.send_json(200, {"ok": True, **result})

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
        full_path = os.path.normpath(os.path.join(STATIC_DIR, relative))
        if not full_path.startswith(STATIC_DIR) or not os.path.isfile(full_path):
            return self.send_text(404, "not found")
        if os.path.basename(full_path) in (DB_FILE, AUTH_FILE):
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
        webbrowser.open(f"http://127.0.0.1:{port}/login.html")
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
    init_default_auth()
    # 启动后确保账户库至少有一份初始备份（迁移 / 首次部署），避免零备份状态
    if not os.path.isdir(AUTH_BACKUP_ROOT) or not any(
        entry.is_dir(follow_symlinks=False) and os.path.isfile(os.path.join(entry.path, AUTH_FILE))
        for entry in os.scandir(AUTH_BACKUP_ROOT)
    ):
        create_auth_backup("init")
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
