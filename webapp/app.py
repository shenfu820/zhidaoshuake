#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""神芙刷课站 · 后端（用户端 + 管理后台）

开发:  python webapp/app.py --port 8778
生产:  gunicorn -w 1 --threads 32 --timeout 0 -b 127.0.0.1:8778 webapp.app:app
       （必须单 worker：任务注册表与二维码会话在进程内存里）
"""
import argparse
import os
import re
import secrets
import sys
import time
from functools import wraps

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from flask import (Flask, g, jsonify, redirect, request,  # noqa: E402
                   send_from_directory, session)

import runner  # noqa: E402
import sechelp  # noqa: E402
import store  # noqa: E402
import ai as ai_mod  # noqa: E402


def _secret_key() -> str:
    env = os.environ.get("ZHS_SECRET")
    if env:
        return env
    path = os.path.join(ROOT, "data", ".devsecret")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        return open(path, encoding="utf-8").read().strip()
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as f:
        f.write(key)
    return key


app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")
app.secret_key = _secret_key()
try:
    app.json.ensure_ascii = False
except Exception:
    app.config["JSON_AS_ASCII"] = False
store.init()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------- 基础 ----------------
def body():
    return request.get_json(silent=True) or {}


def client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or ""


def err(msg, code=400):
    return jsonify({"error": msg}), code


def need_login(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        uid = session.get("uid")
        if not uid:
            return err("未登录", 401)
        user = store.get_user(uid)
        if not user:
            session.clear()
            return err("会话失效，请重新登录", 401)
        g.user = user
        return fn(*a, **kw)
    return wrapper


def need_admin(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        uid = session.get("uid")
        if not uid:
            return err("未登录", 401)
        user = store.get_user(uid)
        if not user:
            session.clear()
            return err("会话失效，请重新登录", 401)
        if user["role"] != "admin":
            return err("需要管理员权限", 403)
        g.user = user
        return fn(*a, **kw)
    return wrapper


# ---------------- 页面 ----------------
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/admin")
def admin_page():
    # 管理后台入口：非管理员直接打回首页，不给任何后台外壳
    uid = session.get("uid")
    user = store.get_user(uid) if uid else None
    if not user or user["role"] != "admin":
        return redirect("/?denied=admin")
    return send_from_directory(app.static_folder, "index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "ts": int(time.time())})


# ---------------- 账号 ----------------
@app.post("/api/register")
def register():
    if store.get_setting("allow_register", "1") != "1":
        return err("站点已关闭注册", 403)
    d = body()
    email = (d.get("email") or "").strip().lower()
    pw = d.get("password") or ""
    if not EMAIL_RE.match(email):
        return err("邮箱格式不对")
    if len(pw) < 8:
        return err("密码至少 8 位")
    if store.get_user_by_email(email):
        return err("该邮箱已注册", 409)
    uid = store.create_user(email, sechelp.hash_password(pw))
    session["uid"] = uid
    store.touch_login(uid)
    store.audit(uid, email, "register", "", client_ip())
    return jsonify({"ok": True, "email": email})


@app.post("/api/login")
def login():
    d = body()
    email = (d.get("email") or "").strip().lower()
    pw = d.get("password") or ""
    row = store.get_user_by_email(email)
    if not row or not sechelp.verify_password(pw, row["pw"]):
        store.audit(row["id"] if row else None, email, "login_failed", "", client_ip())
        return err("邮箱或密码不对", 401)
    session["uid"] = row["id"]
    store.touch_login(row["id"])
    store.audit(row["id"], email, "login", "", client_ip())
    return jsonify({"ok": True, "email": row["email"], "role": row["role"]})


@app.post("/api/logout")
def logout():
    uid = session.get("uid")
    if uid:
        u = store.get_user(uid)
        store.audit(uid, u["email"] if u else None, "logout", "", client_ip())
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
@need_login
def me():
    u = g.user
    z = store.get_zhs(u["id"])
    return jsonify({
        "id": u["id"],
        "email": u["email"],
        "role": u["role"],
        "created": u["created"],
        "last_login": u["last_login"],
        "zhs": {
            "bound": bool(z),
            "label": z["label"] if z else None,
            "updated": z["updated"] if z else None,
        },
        "ai": {
            "enabled": bool(ai_mod.public_view()["enabled"] and ai_mod.public_view()["has_key"]),
            "model": ai_mod.public_view()["model"],
        },
        "stats": store.user_stats(u["id"]),
        "settings": {
            "site_name": store.get_setting("site_name", "神芙刷课站"),
            "announcement": store.get_setting("announcement", ""),
            "max_tasks_per_user": int(store.get_setting("max_tasks_per_user", "2")),
            "max_concurrent": int(store.get_setting("max_concurrent", "3")),
            "allow_register": store.get_setting("allow_register", "1") == "1",
        },
    })


@app.post("/api/me/password")
@need_login
def change_password():
    d = body()
    old, new = d.get("old") or "", d.get("new") or ""
    if not sechelp.verify_password(old, g.user["pw"]):
        return err("原密码不对")
    if len(new) < 8:
        return err("新密码至少 8 位")
    if old == new:
        return err("新密码不能和原密码相同")
    store.set_password(g.user["id"], sechelp.hash_password(new))
    store.audit(g.user["id"], g.user["email"], "password_change", "", client_ip())
    return jsonify({"ok": True})


# ---------------- 智慧树绑定 ----------------
@app.post("/api/zhs/qr/start")
@need_login
def zhs_qr_start():
    sid = runner.qr_start(g.user["id"])
    return jsonify({"sid": sid})


@app.get("/api/zhs/qr/<sid>")
@need_login
def zhs_qr_poll(sid):
    s = runner.qr_get(sid)
    if not s or s["user_id"] != g.user["id"]:
        return err("会话不存在或已过期", 404)
    img = s.get("img")
    if s["status"] == "success":
        store.audit(g.user["id"], g.user["email"], "zhs_bind", s.get("msg", ""), client_ip())
    return jsonify({
        "status": s["status"],
        "msg": s["msg"],
        "img": ("data:image/png;base64," + img) if img else None,
    })


@app.post("/api/zhs/unbind")
@need_login
def zhs_unbind():
    store.delete_zhs(g.user["id"])
    store.audit(g.user["id"], g.user["email"], "zhs_unbind", "", client_ip())
    return jsonify({"ok": True})


# ---------------- 课程 ----------------
@app.get("/api/courses")
@need_login
def courses():
    try:
        return jsonify(runner.fetch_courses(g.user["id"]))
    except Exception as e:
        return err(str(e))


# ---------------- 任务 ----------------
KIND_RE = re.compile(r"[a-zA-Z]")
# 任务 id 的形状：uuid4().hex[:16]，纯 16 位小写十六进制。
# 前端「重跑」曾经把任务 id 当 course_id 提交，智慧树接口回 code -1「参数解码发生错误」，
# 任务 10 秒内必失败——这里在入口就挡掉，别让它排队烧风控额度。
TASK_ID_RE = re.compile(r"[0-9a-f]{16}")


@app.post("/api/tasks")
@need_login
def create_task():
    uid = g.user["id"]
    d = body()
    course_id = (d.get("course_id") or "").strip()
    course_name = (d.get("course_name") or "").strip()[:200]
    if not course_id:
        return err("缺少课程 ID")
    if TASK_ID_RE.fullmatch(course_id):
        return err("course_id 是任务 ID，不是课程 ID；请从课程库重新发起", 400)
    if len(course_id) > 120:
        return err("课程 ID 过长", 400)
    if not store.get_zhs(uid):
        return err("请先绑定智慧树账号")
    limit = int(store.get_setting("max_tasks_per_user", "2"))
    if store.running_count(uid) >= limit:
        return err(f"你已有 {limit} 个任务在跑，先等它完成", 429)
    # 同一账号同时刷多门课是最扎眼的风控信号，默认强制串行
    if int(store.get_setting("one_task_per_user", "1")):
        for t in store.list_tasks(uid):
            if t["status"] in ("running", "throttled", "queued"):
                return err(
                    f"同一智慧树账号同时只能刷一门课，"
                    f"「{t['course_name'] or t['course_id']}」还在跑，先停掉它或等它完成", 429)
    if "|" in course_id:
        kind = "ai"
    elif KIND_RE.search(course_id):
        kind = "zhidao"
    else:
        kind = "hike"
    tid = store.create_task(uid, course_id, course_name, kind)
    runner.start_task(tid)
    store.audit(uid, g.user["email"], "task_create", f"{course_name} / {course_id}", client_ip())
    return jsonify({"ok": True, "task_id": tid})


@app.get("/api/tasks")
@need_login
def tasks():
    return jsonify([dict(r) for r in store.list_tasks(g.user["id"])])


def _task_row(tid):
    """管理员在「全部任务」页也要能看/停别人的任务，否则点日志只会 404「任务不存在」。"""
    scope = None if g.user.get("role") == "admin" else g.user["id"]
    return store.get_task(tid, scope)


@app.get("/api/tasks/<tid>")
@need_login
def task_detail(tid):
    row = _task_row(tid)
    if not row:
        return err("任务不存在", 404)
    d = dict(row)
    if request.args.get("log") != "1":
        d.pop("log", None)
    d["running"] = runner.is_running(tid)
    return jsonify(d)


@app.post("/api/tasks/<tid>/stop")
@need_login
def task_stop(tid):
    row = _task_row(tid)
    if not row:
        return err("任务不存在", 404)
    ok = runner.stop_task(tid)
    if not ok:
        store.update_task(tid, status="stopped")
    store.audit(g.user["id"], g.user["email"], "task_stop", tid, client_ip())
    return jsonify({"ok": True, "signaled": ok})


# ---------------- 管理后台 ----------------
@app.get("/api/admin/overview")
@need_admin
def admin_overview():
    stats = store.site_stats()
    stats["recent_tasks"] = [dict(r) for r in store.list_all_tasks(limit=8)]
    stats["recent_audit"] = [dict(r) for r in store.list_audit(limit=8)]
    stats["settings"] = store.get_settings()
    stats["server"] = {
        "now": time.strftime("%Y-%m-%d %H:%M:%S"),
        "uptime": int(time.time() - START_TS),
        "python": sys.version.split()[0],
    }
    return jsonify(stats)


@app.get("/api/admin/users")
@need_admin
def admin_users():
    return jsonify([dict(r) for r in store.list_users()])


@app.post("/api/admin/users/<int:uid>/role")
@need_admin
def admin_set_role(uid):
    role = (body().get("role") or "").strip()
    if role not in ("user", "admin"):
        return err("角色只能是 user 或 admin")
    target = store.get_user(uid)
    if not target:
        return err("用户不存在", 404)
    if target["role"] == "admin" and role == "user" and store.count_admins() <= 1:
        return err("至少保留一名管理员")
    store.set_role(uid, role)
    store.audit(g.user["id"], g.user["email"], "admin_set_role",
                f"{target['email']} -> {role}", client_ip())
    return jsonify({"ok": True})


@app.delete("/api/admin/users/<int:uid>")
@need_admin
def admin_delete_user(uid):
    target = store.get_user(uid)
    if not target:
        return err("用户不存在", 404)
    if target["id"] == g.user["id"]:
        return err("不能删除自己")
    if target["role"] == "admin" and store.count_admins() <= 1:
        return err("至少保留一名管理员")
    store.delete_user(uid)
    store.audit(g.user["id"], g.user["email"], "admin_delete_user", target["email"], client_ip())
    return jsonify({"ok": True})


@app.get("/api/admin/tasks")
@need_admin
def admin_tasks():
    status = request.args.get("status") or None
    return jsonify([dict(r) for r in store.list_all_tasks(status=status)])


@app.post("/api/admin/tasks/<tid>/stop")
@need_admin
def admin_stop_task(tid):
    row = store.get_task(tid)
    if not row:
        return err("任务不存在", 404)
    ok = runner.stop_task(tid)
    if not ok:
        store.update_task(tid, status="stopped")
    store.audit(g.user["id"], g.user["email"], "admin_stop_task", tid, client_ip())
    return jsonify({"ok": True, "signaled": ok})


@app.get("/api/admin/audit")
@need_admin
def admin_audit():
    return jsonify([dict(r) for r in store.list_audit(limit=300)])


@app.get("/api/admin/settings")
@need_admin
def admin_settings_get():
    return jsonify(store.get_settings())


@app.post("/api/admin/settings")
@need_admin
def admin_settings_set():
    d = body()
    clean = {}
    for k, v in d.items():
        if k not in store.DEFAULT_SETTINGS:
            continue
        if k in ("max_concurrent", "max_tasks_per_user"):
            try:
                n = int(v)
            except Exception:
                return err(f"{k} 必须是整数")
            if not (1 <= n <= 50):
                return err(f"{k} 需在 1-50 之间")
            v = n
        if k == "captcha_cooldown":
            try:
                n = int(v)
            except Exception:
                return err("冷却分钟数必须是整数")
            if not (0 <= n <= 240):
                return err("冷却时间需在 0-240 分钟之间")
            v = n
        if k == "stagger_max":
            try:
                n = int(v)
            except Exception:
                return err("错峰秒数必须是整数")
            if not (0 <= n <= 600):
                return err("错峰需在 0-600 秒之间")
            v = n
        if k == "proxy_pool":
            v = str(v or "").strip()
            if len(v) > 4000:
                return err("代理池文本过长（上限 4000 字）")
            bad = [ln.strip() for ln in v.replace(",", "\n").split("\n")
                   if ln.strip() and "://" not in ln]
            if bad:
                return err(f"代理格式应形如 http://user:pass@ip:port，共 {len(bad)} 行不合规")
        if k == "proxy_api_url":
            v = str(v or "").strip()
            if v and not v.startswith(("http://", "https://")):
                return err("代理源 API 需要是 http(s) 链接")
            if len(v) > 500:
                return err("代理源 API 地址过长")
        if k == "one_task_per_user":
            v = "1" if str(v) in ("1", "true", "True", "on") else "0"
        if k == "allow_register":
            v = "1" if str(v) in ("1", "true", "True") else "0"
        clean[k] = v
    store.set_settings(clean)
    store.audit(g.user["id"], g.user["email"], "admin_settings",
                ", ".join(f"{k}={v}" for k, v in clean.items()), client_ip())
    return jsonify({"ok": True, "settings": store.get_settings()})


# ---------------- AI 答题（DeepSeek） ----------------
@app.get("/api/admin/risk")
@need_admin
def admin_risk():
    import runner as _r
    return jsonify(_r.risk_status())


@app.post("/api/admin/risk/clear")
@need_admin
def admin_risk_clear():
    """手动解除风控冷却——必须在服务进程内清，外部改库不生效。"""
    import runner as _r
    _r._risk.clear()
    store.audit(g.user["id"], g.user["email"], "risk_clear", "", client_ip())
    return jsonify({"ok": True, "risk": _r.risk_status()})


@app.post("/api/admin/proxy/fetch")
@need_admin
def admin_proxy_fetch():
    """从代理服务商 API 拉取、探活，把可用的写进代理池。"""
    import proxypool
    d = body()
    url = (d.get("api_url") or store.get_setting("proxy_api_url", "") or "").strip()
    if not url.startswith(("http://", "https://")):
        return err("代理源 API 需要是 http(s) 链接")
    try:
        limit = int(d.get("limit") or 60)
    except Exception:
        limit = 60
    limit = max(1, min(200, limit))
    try:
        res = proxypool.refresh(url, limit=limit)
    except Exception as e:
        return err(f"拉取失败：{e}")
    if res["proxies"]:
        store.set_setting("proxy_pool", "\n".join(res["proxies"]))
        store.set_setting("proxy_api_url", url)
    store.audit(g.user["id"], g.user["email"], "proxy_fetch",
                f"候选 {res['fetched']} / 可用 {res['alive']}", client_ip())
    return jsonify({
        "ok": True,
        "fetched": res["fetched"],
        "alive": res["alive"],
        "dead": res["dead"],
        "written": len(res["proxies"]),
        "sample": res["proxies"][:8],
        "latency": dict(list(res["latency"].items())[:8]),
    })


@app.post("/api/admin/proxy/check")
@need_admin
def admin_proxy_check():
    """对当前代理池做一次探活，剔除连不通的。"""
    import proxypool
    import runner as _r
    cur = _r.proxy_pool()
    if not cur:
        return err("代理池是空的")
    good = proxypool.check_many(cur)
    keep = [p for p, _ in good]
    store.set_setting("proxy_pool", "\n".join(keep))
    return jsonify({"ok": True, "before": len(cur), "after": len(keep),
                    "alive": keep[:8]})


@app.get("/api/admin/ai")
@need_admin
def admin_ai_get():
    return jsonify(ai_mod.public_view())


@app.post("/api/admin/ai")
@need_admin
def admin_ai_set():
    d = body()
    patch = {
        "enabled": bool(d.get("enabled")),
        "base_url": d.get("base_url") or "https://api.deepseek.com",
        "model": d.get("model") or "deepseek-chat",
        "api_key": d.get("api_key") or "",
        "clear_key": bool(d.get("clear_key")),
    }
    cfg = ai_mod.save(patch)
    store.audit(g.user["id"], g.user["email"], "admin_ai_config",
                f"{cfg['model']} @ {cfg['base_url']} enabled={cfg['enabled']}",
                client_ip())
    return jsonify({"ok": True, "ai": ai_mod.public_view()})


@app.post("/api/admin/ai/test")
@need_admin
def admin_ai_test():
    """用一道一定能答对的小题验证 key / 模型 / 网络是否通。"""
    answerer = ai_mod.Answerer()
    if not answerer.available():
        return err("AI 未启用或缺少密钥")
    t0 = time.time()
    ids = answerer.answer(
        "下列哪个数是偶数？",
        [{"id": 101, "content": "3"}, {"id": 102, "content": "8"}, {"id": 103, "content": "5"}],
    )
    ms = int((time.time() - t0) * 1000)
    ok = ids == ["102"]
    store.audit(g.user["id"], g.user["email"], "admin_ai_test",
                f"{'pass' if ok else 'fail'} {ids} {ms}ms", client_ip())
    return jsonify({"ok": ok, "picked": ids, "expect": ["102"], "ms": ms,
                    "model": ai_mod.public_view()["model"]})


START_TS = time.time()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8778)))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()