#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无头浏览器控制：按需启动 headless_shell，用 CDP 驱动，用完即关。

用途：撞到人机验证时，用已保存的 cookie 打开真实页面，把验证画面截下来，
或者在有可解形态（图片码/滑块）时自动处理。

设计要点：
  - 不常驻。每次用完 terminate()，避免在 1.8G 内存的机器上白占资源。
  - 用 unix socket 或 127.0.0.1 随机端口，只监听回环。
  - CDP 只走本机，不对外暴露。
"""
import base64
import json
import os
import random
import shutil
import socket
import subprocess
import tempfile
import time

import requests as _requests

HEADLESS_BIN = "/usr/lib64/chromium-browser/headless_shell"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Browser:
    """一次性的无头浏览器会话。"""

    def __init__(self, timeout=45, user_agent=None):
        self.port = _free_port()
        self.timeout = timeout
        # UA 必须与刷课时一致：智慧树的 acw_tc 是阿里云 WAF 的指纹 cookie，
        # 会校验 UA/指纹是否与签发时相同，用 HeadlessChrome 直接被打回登录页。
        self.user_agent = user_agent
        self.proc = None
        self.profile = tempfile.mkdtemp(prefix="hs-")
        self.ws = None
        self._msg_id = 0
        self.target_id = None

    # ---------- 生命周期 ----------
    def start(self):
        if not os.path.exists(HEADLESS_BIN):
            raise RuntimeError(f"没找到 {HEADLESS_BIN}，先装 chromium-headless")
        args = [
            HEADLESS_BIN,
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-software-rasterizer",
            "--disable-extensions",
            "--mute-audio",
            "--no-first-run",
            "--window-size=1280,900",
            f"--user-data-dir={self.profile}",
            f"--remote-debugging-port={self.port}",
            "about:blank",
        ]
        if self.user_agent:
            args.insert(-1, f"--user-agent={self.user_agent}")
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                r = _requests.get(f"http://127.0.0.1:{self.port}/json/version", timeout=1)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.25)
        else:
            self.stop()
            raise RuntimeError("浏览器启动超时")
        self._attach()
        return self

    def _attach(self):
        import websocket  # websocket-client
        pages = _requests.get(f"http://127.0.0.1:{self.port}/json/list", timeout=5).json()
        page = next((p for p in pages if p.get("type") == "page"), None)
        if page is None:
            page = _requests.put(
                f"http://127.0.0.1:{self.port}/json/new?about:blank", timeout=5).json()
        self.target_id = page["id"]
        self.ws = websocket.create_connection(
            page["webSocketDebuggerUrl"], timeout=self.timeout,
            suppress_origin=True)
        self.call("Page.enable")
        self.call("Network.enable")

    def stop(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass
        shutil.rmtree(self.profile, ignore_errors=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()

    # ---------- CDP ----------
    def call(self, method, **params):
        self._msg_id += 1
        mid = self._msg_id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                raw = self.ws.recv()
            except Exception:
                time.sleep(0.05)
                continue
            if not raw:
                continue
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"{method} 超时")

    # ---------- 高层操作 ----------
    def set_cookies(self, cookies):
        """cookies: 智慧树导出的 list，字段 name/value/domain 即可。"""
        fixed = []
        for c in cookies or []:
            name = (c.get("name") or "").strip()
            dom = (c.get("domain") or "").strip()
            val = c.get("value")
            # 没有域名或没有名字的条目会被 CDP 拒绝，直接跳过
            if not name or not dom or val is None:
                continue
            if not dom.startswith("."):
                dom = "." + dom.lstrip(".")
            if len(dom) < 3:      # 只剩一个 "."，非法
                continue
            item = {
                "name": name,
                "value": str(val),
                "domain": dom,
                "path": c.get("path") or "/",
            }
            # 属性必须原样带过去：CASTGC 是 SameSite=None + Secure 的组合，
            # 丢了 SameSite 浏览器按 Lax 处理，跨子域跳转时不发送，登录态直接丢。
            rest = c.get("rest") or {}
            if c.get("secure") or rest.get("SameSite") == "None":
                item["secure"] = True
            if rest.get("HttpOnly"):
                item["httpOnly"] = True
            ss = rest.get("SameSite")
            if ss in ("Strict", "Lax", "None"):
                item["sameSite"] = ss
            exp = c.get("expires")
            if exp:
                try:
                    item["expires"] = float(exp)
                except Exception:
                    pass
            fixed.append(item)
        if fixed:
            self.call("Network.setCookies", cookies=fixed)
        return len(fixed)

    def set_ua(self, ua):
        """在页面层再覆盖一次 UA，和启动参数双保险。"""
        self.call("Network.setUserAgentOverride", userAgent=ua)

    def goto(self, url, wait=3.0, tolerant=True):
        """导航。慢站（如 onlineweb）的 navigate 应答可能超时，
        但页面其实已经在加载了，所以默认容忍超时后继续等待。"""
        try:
            self.call("Page.navigate", url=url)
        except TimeoutError:
            if not tolerant:
                raise
        time.sleep(wait)

    def eval_js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr,
                      returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    def screenshot(self, path=None):
        r = self.call("Page.captureScreenshot", format="png")
        data = base64.b64decode(r["data"])
        if path:
            with open(path, "wb") as f:
                f.write(data)
        return data

    def html(self):
        return self.eval_js("document.documentElement.outerHTML") or ""

    def title(self):
        return self.eval_js("document.title") or ""