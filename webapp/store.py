#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 持久层：用户 / 智慧树绑定 / 刷课任务 / 审计 / 运行时配置。"""
import json
import os
import sqlite3
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("ZHS_DB") or os.path.join(ROOT, "data", "zhsweb.db")

_local = threading.local()
_init_lock = threading.Lock()

DEFAULT_SETTINGS = {
    "site_name": "神芙刷课站",
    "allow_register": "1",
    # 每个任务绝大部分时间在 sleep 等 HTTP，2 核 2G 的机器跑 6 路毫无压力；
    # 真正的天花板是单出口 IP 的撞码概率，要再往上必须先配代理池（proxy_pool）。
    "max_concurrent": "6",
    "max_tasks_per_user": "2",
    "captcha_cooldown": "8",
    "stagger_max": "30",
    "proxy_pool": "",
    "proxy_api_url": "",
    "one_task_per_user": "1",
    "announcement": "",
}

_TASK_STATES = ("queued", "throttled", "running", "done", "failed",
                "stopped", "need_captcha", "interrupted")


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=20000")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def init():
    with _init_lock:
        c = conn()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                pw TEXT NOT NULL,
                created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS zhs (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                label TEXT,
                cookies BLOB NOT NULL,
                updated TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                course_id TEXT NOT NULL,
                course_name TEXT,
                kind TEXT,
                status TEXT NOT NULL,
                done INTEGER DEFAULT 0,
                total INTEGER,
                log TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created TEXT NOT NULL,
                updated TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, created DESC);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                user_id INTEGER,
                email TEXT,
                action TEXT NOT NULL,
                detail TEXT,
                ip TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        # 迁移：老库补 role 列
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
        if "role" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        if "last_login" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN last_login TEXT")
        # 当前视频的实时进度：写 "百分比|已播秒|总秒|倍速"
        cols = [r[1] for r in c.execute("PRAGMA table_info(tasks)").fetchall()]
        if "vprog" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN vprog TEXT")
        # 默认配置
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)", (k, v))
        # 重启后遗留的活跃任务统一标为 interrupted。
        # throttled 必须算进来：它表示「正在等风控冷却」，等冷却的线程活在旧进程里，
        # 进程一重启线程就没了，状态会永久卡在「冷却中」再也醒不过来。
        c.execute(
            "UPDATE tasks SET status='interrupted', updated=? "
            "WHERE status IN ('queued','running','throttled')",
            (_now(),),
        )
        c.commit()


# ---------------- users ----------------
def create_user(email: str, pw_hash: str, role: str = None):
    c = conn()
    if role is None:
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        role = "admin" if total == 0 else "user"   # 首个注册者是管理员
    cur = c.execute(
        "INSERT INTO users(email, pw, created, role) VALUES(?,?,?,?)",
        (email, pw_hash, _now(), role),
    )
    c.commit()
    return cur.lastrowid


def get_user_by_email(email: str):
    return conn().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def get_user(uid: int):
    return conn().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def touch_login(uid: int):
    c = conn()
    c.execute("UPDATE users SET last_login=? WHERE id=?", (_now(), uid))
    c.commit()


def set_password(uid: int, pw_hash: str):
    c = conn()
    c.execute("UPDATE users SET pw=? WHERE id=?", (pw_hash, uid))
    c.commit()


def set_role(uid: int, role: str):
    c = conn()
    c.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    c.commit()


def delete_user(uid: int):
    c = conn()
    c.execute("DELETE FROM users WHERE id=?", (uid,))
    c.commit()


def count_admins() -> int:
    return conn().execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]


def list_users(limit: int = 200):
    return conn().execute(
        """SELECT u.id, u.email, u.role, u.created, u.last_login,
                  (SELECT COUNT(*) FROM tasks t WHERE t.user_id=u.id) AS task_count,
                  (SELECT COUNT(*) FROM tasks t WHERE t.user_id=u.id
                     AND t.status IN ('queued','running')) AS active_count,
                  (SELECT label FROM zhs z WHERE z.user_id=u.id) AS zhs_label
           FROM users u ORDER BY u.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()


# ---------------- zhs binding ----------------
def save_zhs(user_id: int, label: str, cookies_blob: bytes):
    c = conn()
    c.execute(
        """INSERT INTO zhs(user_id, label, cookies, updated) VALUES(?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET label=excluded.label,
             cookies=excluded.cookies, updated=excluded.updated""",
        (user_id, label, cookies_blob, _now()),
    )
    c.commit()


def get_zhs(user_id: int):
    return conn().execute("SELECT * FROM zhs WHERE user_id=?", (user_id,)).fetchone()


def delete_zhs(user_id: int):
    c = conn()
    c.execute("DELETE FROM zhs WHERE user_id=?", (user_id,))
    c.commit()


# ---------------- tasks ----------------
def create_task(user_id: int, course_id: str, course_name: str, kind: str) -> str:
    tid = uuid.uuid4().hex[:16]
    now = _now()
    c = conn()
    c.execute(
        """INSERT INTO tasks(id, user_id, course_id, course_name, kind, status,
                             done, total, log, created, updated)
           VALUES(?,?,?,?,?,?,0,NULL,'',?,?)""",
        (tid, user_id, course_id, course_name, kind, "queued", now, now),
    )
    c.commit()
    return tid


def get_task(task_id: str, user_id: int = None):
    if user_id is None:
        return conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return conn().execute(
        "SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, user_id)
    ).fetchone()


def list_tasks(user_id: int, limit: int = 40):
    return conn().execute(
        "SELECT id, course_id, course_name, kind, status, done, total, error, created, updated, vprog "
        "FROM tasks WHERE user_id=? ORDER BY created DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()


def list_all_tasks(limit: int = 200, status: str = None):
    sql = """SELECT t.id, t.course_id, t.course_name, t.kind, t.status, t.done, t.total,
                    t.error, t.created, t.updated, t.vprog, u.email
             FROM tasks t LEFT JOIN users u ON u.id=t.user_id"""
    args = []
    if status:
        sql += " WHERE t.status=?"
        args.append(status)
    sql += " ORDER BY t.created DESC LIMIT ?"
    args.append(limit)
    return conn().execute(sql, args).fetchall()


def update_task(task_id: str, **fields):
    if not fields:
        return
    fields["updated"] = _now()
    keys = ", ".join(f"{k}=?" for k in fields)
    c = conn()
    c.execute(f"UPDATE tasks SET {keys} WHERE id=?", (*fields.values(), task_id))
    c.commit()


def append_log(task_id: str, text: str, cap_lines: int = 600):
    if not text:
        return
    c = conn()
    row = c.execute("SELECT log FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return
    lines = (row["log"] or "").splitlines() + text.splitlines()
    if len(lines) > cap_lines:
        lines = lines[-cap_lines:]
    c.execute("UPDATE tasks SET log=?, updated=? WHERE id=?",
              ("\n".join(lines), _now(), task_id))
    c.commit()


def running_count(user_id: int) -> int:
    return conn().execute(
        "SELECT COUNT(*) FROM tasks WHERE user_id=? AND status IN ('queued','running')",
        (user_id,),
    ).fetchone()[0]


def global_running_count() -> int:
    return conn().execute(
        "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','running')"
    ).fetchone()[0]


# ---------------- 统计 ----------------
def user_stats(user_id: int) -> dict:
    c = conn()
    rows = c.execute(
        "SELECT status, COUNT(*) n FROM tasks WHERE user_id=? GROUP BY status", (user_id,)
    ).fetchall()
    by = {r["status"]: r["n"] for r in rows}
    return {
        "total": sum(by.values()),
        "running": by.get("running", 0) + by.get("queued", 0),
        "done": by.get("done", 0),
        "failed": by.get("failed", 0),
        "stopped": by.get("stopped", 0),
        "need_captcha": by.get("need_captcha", 0),
    }


def site_stats() -> dict:
    c = conn()
    users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    bound = c.execute("SELECT COUNT(*) FROM zhs").fetchone()[0]
    rows = c.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status").fetchall()
    by = {r["status"]: r["n"] for r in rows}
    videos = c.execute("SELECT COALESCE(SUM(done),0) FROM tasks").fetchone()[0]
    today = time.strftime("%Y-%m-%d")
    today_tasks = c.execute(
        "SELECT COUNT(*) FROM tasks WHERE created LIKE ?", (today + "%",)
    ).fetchone()[0]
    return {
        "users": users,
        "bound": bound,
        "unbound": users - bound,
        "tasks_total": sum(by.values()),
        "tasks_running": by.get("running", 0) + by.get("queued", 0),
        "tasks_done": by.get("done", 0),
        "tasks_failed": by.get("failed", 0),
        "tasks_captcha": by.get("need_captcha", 0),
        "videos_done": int(videos or 0),
        "today_tasks": today_tasks,
        "by_status": by,
    }


# ---------------- 审计 ----------------
def audit(user_id, email, action, detail="", ip=""):
    c = conn()
    c.execute(
        "INSERT INTO audit(ts, user_id, email, action, detail, ip) VALUES(?,?,?,?,?,?)",
        (_now(), user_id, email, action, str(detail)[:500], ip or ""),
    )
    c.commit()


def list_audit(limit: int = 200):
    return conn().execute(
        "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


# ---------------- 配置 ----------------
def get_settings() -> dict:
    rows = conn().execute("SELECT key, value FROM settings").fetchall()
    data = dict(DEFAULT_SETTINGS)
    data.update({r["key"]: r["value"] for r in rows})
    return data


def get_setting(key: str, default=None):
    row = conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    c = conn()
    c.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    c.commit()


def set_settings(pairs: dict):
    c = conn()
    for k, v in pairs.items():
        if k in DEFAULT_SETTINGS:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, str(v)),
            )
    c.commit()