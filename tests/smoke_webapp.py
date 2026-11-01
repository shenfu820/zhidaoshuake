#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""webapp 冒烟测试：不接触真实智慧树账号，只验证
   注册 → 注入假 cookie → 建任务 → 线程执行 → 状态/日志落库 这条链路。
"""
import json
import os
import random
import sys
import time
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

import app as webapp  # noqa: E402
import sechelp  # noqa: E402
import store  # noqa: E402

EMAIL = f"smoke{random.randint(10000, 99999)}@local.test"
PASSWORD = "passw0rd123"


def fake_cookie_list():
    payload = quote(json.dumps({"uuid": "deadbeefcafe1234", "realName": "冒烟测试"}))
    return [{
        "name": "CASLOGC", "value": payload, "domain": ".zhihuishu.com",
        "port": None, "path": "/", "expires": None, "secure": True,
        "rest": {"HttpOnly": None}, "version": 0, "comment": None,
        "comment_url": None, "rfc2109": False, "discard": True,
    }]


def main():
    c = webapp.app.test_client()

    r = c.post("/api/register", json={"email": EMAIL, "password": PASSWORD})
    print("1) register:", r.status_code, r.get_json())

    me = c.get("/api/me").get_json()
    print("2) me:", json.dumps(me, ensure_ascii=False))

    uid = store.get_user_by_email(EMAIL)["id"]
    store.save_zhs(uid, "冒烟测试账号",
                   sechelp.encrypt(json.dumps(fake_cookie_list())))
    me = c.get("/api/me").get_json()
    print("3) bound:", me["zhs"]["bound"], me["zhs"]["label"])

    r = c.post("/api/tasks", json={"course_id": "fakeCourseIdAbc", "course_name": "冒烟测试课程"})
    print("4) create task:", r.status_code, r.get_json())
    tid = r.get_json().get("task_id")

    for _ in range(20):
        time.sleep(1)
        d = c.get(f"/api/tasks/{tid}?log=1").get_json()
        if d["status"] not in ("queued", "running"):
            break

    print("5) final status:", d["status"])
    print("6) error:", (d.get("error") or "")[:160])
    print("7) log tail:")
    for line in (d.get("log") or "").splitlines()[-6:]:
        print("   ", line)

    lst = c.get("/api/tasks").get_json()
    print("8) task list size:", len(lst), "| first:", lst[0]["course_name"], lst[0]["status"])


if __name__ == "__main__":
    main()