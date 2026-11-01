#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代理池：从服务商 API 拉取、解析、探活，再写进设置。

不内置任何代理 IP——IP 要你自己从服务商买。这里只负责：
  1. 把你服务商给的那条 API 链接拉下来
  2. 兼容几种常见的返回格式，抠出 ip:port
  3. 并发探活，只留真正能连通的
  4. 写回 settings.proxy_pool

支持的返回格式：
  - 纯文本，每行一条 1.2.3.4:8080
  - 纯文本，1.2.3.4:8080\r\n 混排
  - JSON: {"data":["1.2.3.4:8080", ...]}
  - JSON: {"data":{"proxy_list":["1.2.3.4:8080"]}}
  - JSON: {"result":[{"ip":"1.2.3.4","port":8080}, ...]}
"""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import requests as _requests

_S = _requests.Session()
_S.trust_env = False
_S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

# 探活目标：智慧树的接口，能连通才算对刷课有用
PROBE_URL = "https://www.zhihuishu.com/"
IP_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})\s*:\s*(\d{2,5})")
URL_RE = re.compile(r"https?://[^\s\"'<>,;|)\]}]+")


def _walk_strings(obj, out):
    """从任意 JSON 结构里捞出所有可能含 ip:port 的字符串。"""
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        # {"ip": "1.2.3.4", "port": 8080} 这种拆开的写法
        ip = obj.get("ip") or obj.get("host") or obj.get("proxy_ip")
        port = obj.get("port") or obj.get("proxy_port")
        if ip and port:
            out.append(f"{ip}:{port}")
        for v in obj.values():
            _walk_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_strings(v, out)


def parse(text: str) -> list:
    """把服务商返回的任意内容解析成 ['ip:port', ...]。"""
    found = []
    raw = (text or "").strip()
    if not raw:
        return found

    # 1) 先抓带 scheme 的完整 URL——必须保留 user:pass，否则认证代理直接失效
    rest = raw
    for m in URL_RE.finditer(raw):
        u = m.group(0).rstrip(".,;")
        if IP_RE.search(u):
            found.append(u)
            rest = rest.replace(m.group(0), " ")

    # 先按 JSON 试一次
    try:
        j = json.loads(raw)
        chunks = []
        _walk_strings(j, chunks)
        for c in chunks:
            if c.startswith(("http://", "https://")) and IP_RE.search(c):
                continue          # 已经在上面收过了，别重复也别降级
            for m in IP_RE.finditer(c):
                found.append(f"{m.group(1)}:{m.group(2)}")
    except Exception:
        pass

    # 2) 剩下的裸 ip:port（纯文本格式走这里；已挖掉的部分不会重复）
    for m in IP_RE.finditer(rest):
        found.append(f"{m.group(1)}:{m.group(2)}")

    # 去重保序
    seen, out = set(), []
    for p in found:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _with_scheme(p: str, scheme: str = "http") -> str:
    return p if "://" in p else f"{scheme}://{p}"


def check_one(proxy: str, timeout: float = 8.0) -> tuple:
    """返回 (proxy, ok, 延迟秒)。失败时延迟为 None。"""
    url = _with_scheme(proxy)
    import time
    t0 = time.time()
    try:
        r = _S.get(PROBE_URL, proxies={"http": url, "https": url},
                   timeout=timeout, allow_redirects=False)
        ok = r.status_code < 500
        return (proxy, ok, round(time.time() - t0, 2) if ok else None)
    except Exception:
        return (proxy, False, None)


def check_many(proxies: list, workers: int = 20, timeout: float = 8.0) -> list:
    """并发探活，返回可用的 [(proxy, 延迟), ...] 按延迟升序。"""
    if not proxies:
        return []
    good = []
    lock = threading.Lock()

    def job(p):
        _, ok, ms = check_one(p, timeout)
        if ok:
            with lock:
                good.append((p, ms))

    with ThreadPoolExecutor(max_workers=min(workers, len(proxies))) as ex:
        list(ex.map(job, proxies))
    good.sort(key=lambda x: (x[1] is None, x[1]))
    return good


def fetch(api_url: str, timeout: float = 25.0) -> list:
    """从服务商 API 拉一批候选代理（未探活）。"""
    r = _S.get(api_url, timeout=timeout)
    r.raise_for_status()
    return parse(r.text)


def refresh(api_url: str, limit: int = 60, workers: int = 20) -> dict:
    """拉取 + 探活 + 落地，返回统计。"""
    cand = fetch(api_url)
    if limit and len(cand) > limit:
        cand = cand[:limit]
    good = check_many(cand, workers=workers)
    return {
        "fetched": len(cand),
        "alive": len(good),
        "dead": len(cand) - len(good),
        "proxies": [p for p, _ in good],
        "latency": {p: ms for p, ms in good},
    }