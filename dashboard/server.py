#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ZHS Fucker 本地可视化看板 —— 纯标准库，零额外依赖。

用法:
    python dashboard/server.py --pid <主进程PID> --port 8777
"""
import argparse
import ctypes
import json
import os
import re
import subprocess
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT, "logs")
QR_DIR = os.path.join(ROOT, "qr")
HERE = os.path.dirname(os.path.abspath(__file__))

LOG_LEVELS = ["critical", "error", "warning", "info", "debug"]
TAIL_LIMIT = {"critical": 60, "error": 120, "warning": 120, "info": 400, "debug": 200}
RE_EVENT = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}):\s+(.*)$")

STATE = {"pid": 0, "started": time.time(), "last_scan": 0.0, "auto": False}


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == 259  # STILL_ACTIVE
        return False
    finally:
        k32.CloseHandle(handle)


def find_main_pid() -> int:
    """定位正在运行的 main.py python 进程（仅在已知 PID 失效时调用）。"""
    cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*main.py*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return 0
    pids = [int(x) for x in out.split() if x.strip().isdigit()]
    return pids[0] if pids else 0


def resolve_pid() -> int:
    now = time.time()
    if STATE["pid"] and pid_alive(STATE["pid"]):
        return STATE["pid"]
    if now - STATE["last_scan"] > 15:
        STATE["last_scan"] = now
        found = find_main_pid()
        if found:
            STATE["pid"] = found
            STATE["auto"] = True
    return STATE["pid"]


def read_tail(path: str, max_bytes: int = 512 * 1024):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return []
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines


def parse_events(limit: int = 400):
    events = []
    for level in LOG_LEVELS:
        path = os.path.join(LOG_DIR, level + ".log")
        lines = read_tail(path)[-TAIL_LIMIT.get(level, 100):]
        for line in lines:
            m = RE_EVENT.match(line)
            if m:
                events.append({
                    "ts": m.group(1),
                    "ms": m.group(2),
                    "level": level,
                    "msg": m.group(3).strip(),
                })
            elif line.strip():
                events.append({"ts": "", "ms": "", "level": level, "msg": line.strip()})
    events.sort(key=lambda e: (e["ts"], e["ms"]))
    return events[-limit:]


def guess_course(events):
    info = {"course_id": None, "mode": None, "lessons": None, "videos": None}
    for e in events:
        msg = e["msg"]
        m = re.search(r"Fucking Zhidao course (\w+)", msg)
        if m:
            info["course_id"], info["mode"] = m.group(1), "智到共享课"
        m = re.search(r"Fucking Hike course (\w+)", msg)
        if m:
            info["course_id"], info["mode"] = m.group(1), "校内学分课(hike)"
        m = re.search(r"(\d+) lessons, (\d+) videos", msg)
        if m:
            info["lessons"], info["videos"] = int(m.group(1)), int(m.group(2))
    return info


def build_status():
    events = parse_events()
    pid = resolve_pid()
    info_log = os.path.join(LOG_DIR, "info.log")
    log_size, log_mtime, idle = 0, None, None
    if os.path.exists(info_log):
        st = os.stat(info_log)
        log_size = st.st_size
        log_mtime = datetime.fromtimestamp(st.st_mtime).strftime("%H:%M:%S")
        idle = round(time.time() - st.st_mtime, 1)

    login_at = None
    for e in events:
        if "Login successful" in e["msg"] or "登录成功" in e["msg"]:
            login_at = e["ts"][11:]
    uptime = None
    if login_at:
        try:
            t = datetime.strptime(login_at, "%H:%M:%S")
            now_dt = datetime.now()
            guess = now_dt.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
            uptime = max(0, round((now_dt - guess).total_seconds()))
        except ValueError:
            pass
    qr_files = []
    if os.path.isdir(QR_DIR):
        qr_files = sorted(
            (f for f in os.listdir(QR_DIR) if f.lower().endswith(".png")),
            key=lambda f: os.path.getmtime(os.path.join(QR_DIR, f)),
            reverse=True,
        )
    alive = pid_alive(pid)
    return {
        "now": datetime.now().strftime("%H:%M:%S"),
        "pid": pid or None,
        "pid_auto": STATE["auto"],
        "alive": alive,
        "log_idle": idle,
        "log_size": log_size,
        "login_at": login_at,
        "uptime": uptime,
        "course": guess_course(events),
        "qr": {"count": len(qr_files), "latest": qr_files[0] if qr_files else None},
        "counts": {lv: len([e for e in events if e["level"] == lv]) for lv in LOG_LEVELS},
        "events": events[-120:],
        "tail": read_tail(info_log)[-80:],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ZHS-Dash/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send(200, json.dumps(build_status(), ensure_ascii=False))
        if path == "/api/qr":
            files = sorted(
                (f for f in os.listdir(QR_DIR) if f.lower().endswith(".png")),
                key=lambda f: os.path.getmtime(os.path.join(QR_DIR, f)),
                reverse=True,
            ) if os.path.isdir(QR_DIR) else []
            if not files:
                return self._send(404, json.dumps({"error": "no qr"}))
            with open(os.path.join(QR_DIR, files[0]), "rb") as f:
                return self._send(200, f.read(), "image/png")
        if path == "/api/log/download":
            chunks = []
            for lv in LOG_LEVELS:
                p = os.path.join(LOG_DIR, lv + ".log")
                if os.path.exists(p):
                    chunks.append(f"===== {lv}.log =====\n" + "\n".join(read_tail(p)))
            body = "\n\n".join(chunks) or "(empty)"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="zhs-logs.txt"')
            data = body.encode("utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if urlparse(self.path).path == "/api/stop":
            pid = resolve_pid()
            if not pid or not pid_alive(pid):
                return self._send(200, json.dumps({"ok": False, "msg": "进程已不存在"}, ensure_ascii=False))
            os.system(f"taskkill /PID {pid} /T /F > NUL 2>&1")
            time.sleep(1.0)
            return self._send(200, json.dumps(
                {"ok": not pid_alive(pid), "msg": "已发送终止信号"}, ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--port", type=int, default=8777)
    args = ap.parse_args()
    STATE["pid"] = args.pid
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"dashboard on http://127.0.0.1:{args.port}/  (watch pid={args.pid})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()