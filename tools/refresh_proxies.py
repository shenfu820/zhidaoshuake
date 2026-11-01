#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键刷新远端刷课站的代理池。

做的事：
  1. SSH 到服务器，把内置的采集脚本传过去
  2. 从多个公开源抓候选 http(s) 代理
  3. 并发探活：必须能真正走通 HTTPS CONNECT 到 https://www.zhihuishu.com/
  4. 透明代理检测：出口 IP 必须与本机不同（否则等于没换 IP）
  5. 复检存活后写进 settings.proxy_pool（应用会按账号哈希固定分配）

用法：
  python tools/refresh_proxies.py              # 默认刷新
  ZHS_HOST=1.2.3.4 ZHS_PASS=xxx python tools/refresh_proxies.py

注意：公开代理寿命很短，建议配合定时任务（每 1–2 小时）跑一次；
池子变空时 runner 会自动回退直连，不会让任务直接失败。
"""
import os
import sys

import paramiko

HOST = os.environ.get("ZHS_HOST", "189.24.68.245")
PORT = int(os.environ.get("ZHS_PORT", "22"))
USER = os.environ.get("ZHS_USER", "root")
PASS = os.environ.get("ZHS_PASS", "I0nGC6bsgM")
APP_DIR = os.environ.get("ZHS_APP_DIR", "/www/wwwroot/shenfu.wiki")
PY = os.environ.get("ZHS_PY", "/opt/shenfu-venv/bin/python")
BASE = "http://127.0.0.1:8778"
ADMIN_EMAIL = os.environ.get("ZHS_ADMIN", "admin@shenfu.wiki")
ADMIN_PASS = os.environ.get("ZHS_ADMIN_PASS", "Shenfu@2YQoUaJC5o")

PAYLOAD = r'''
# -*- coding: utf-8 -*-
import sys, json, time
sys.path.insert(0, "__APP_DIR__/webapp")
import proxypool as pp
import requests
from concurrent.futures import ThreadPoolExecutor

SOURCES = [
    ("monosans",   "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("proxifly-h", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt"),
    ("proxifly-s", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/data.txt"),
    ("thespeedx",  "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("iplocate",   "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt"),
    ("proxyscrape","https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=yes&anonymity=anonymous"),
]

print("== 抓取 ==", flush=True)
cand = []
for name, url in SOURCES:
    try:
        items = pp.fetch(url, timeout=20)
        print("  %-11s %5d" % (name, len(items)), flush=True)
        cand += items
    except Exception as e:
        print("  %-11s FAIL %s" % (name, str(e)[:70]), flush=True)

seen, uniq = set(), []
for p in cand:
    if p not in seen:
        seen.add(p); uniq.append(p)
uniq = uniq[:900]
print("  去重 %d / 测前面 %d 条" % (len(seen), len(uniq)), flush=True)

print("== 探活 ==", flush=True)
t0 = time.time()
good = pp.check_many(uniq, workers=100, timeout=5)
print("  可用 %d / %d，%.0fs" % (len(good), len(uniq), time.time() - t0), flush=True)

try:
    real = requests.get("https://api.ipify.org?format=json", timeout=10).json()["ip"]
except Exception:
    real = "?"
print("  本机出口:", real, flush=True)

print("== 透明代理检测（并行） ==", flush=True)
S = requests.Session(); S.trust_env = False

def probe(item):
    p, ms = item
    url = p if "://" in p else "http://" + p
    try:
        ip = S.get("https://api.ipify.org?format=json",
                   proxies={"http": url, "https": url}, timeout=6).json()["ip"]
    except Exception:
        return None
    if ip == real:
        return None
    return {"proxy": url, "latency": ms, "egress": ip}

with ThreadPoolExecutor(max_workers=32) as ex:
    clean = [c for c in ex.map(probe, good) if c]
seen2, dedup = set(), []
for c in clean:
    if c["proxy"] not in seen2:
        seen2.add(c["proxy"]); dedup.append(c)
dedup.sort(key=lambda x: x["latency"] or 99)
print("  非透明可用 %d 条（去重后 %d）" % (len(clean), len(dedup)), flush=True)

N = 24
pick = [c["proxy"] for c in dedup[:N]]
again = pp.check_many(pick, workers=24, timeout=6)
final = [p if "://" in p else "http://" + p for p, _ in again]
print("  复检存活 %d / %d" % (len(final), len(pick)), flush=True)

if not final:
    print("RESULT EMPTY")
    sys.exit(2)

import urllib.request, http.cookiejar
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
def call(path, d=None):
    m = "POST" if d is not None else "GET"
    r = urllib.request.Request("__BASE__" + path, method=m)
    if d is not None:
        r.add_header("Content-Type", "application/json")
        r.data = json.dumps(d).encode()
    try:
        return json.loads(op.open(r, timeout=30).read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}")
        except Exception:
            return {"http": e.code}

call("/api/login", {"email": "__EMAIL__", "password": "__PASSWORD__"})
res = call("/api/admin/settings", {"proxy_pool": "\n".join(final)})
print("  写入:", res.get("ok"), flush=True)
print("RESULT %d" % len(final))
for p in final:
    print("  " + p, flush=True)
'''
for _k, _v in (("__APP_DIR__", APP_DIR), ("__BASE__", BASE),
               ("__EMAIL__", ADMIN_EMAIL), ("__PASSWORD__", ADMIN_PASS)):
    PAYLOAD = PAYLOAD.replace(_k, _v)


def main() -> int:
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, PORT, USER, PASS, timeout=25,
                allow_agent=False, look_for_keys=False)
    try:
        sftp = cli.open_sftp()
        with sftp.file("/tmp/refresh_proxies.py", "w") as fh:
            fh.write(PAYLOAD)
        sftp.close()

        chan = cli.get_transport().open_session()
        chan.exec_command(f"cd {APP_DIR} && {PY} -u /tmp/refresh_proxies.py; "
                          f"rc=$?; rm -f /tmp/refresh_proxies.py; exit $rc")
        for line in iter(lambda: chan.recv(4096), b""):
            sys.stdout.write(line.decode("utf-8", "ignore"))
            sys.stdout.flush()
        rc = chan.recv_exit_status()
        return 0 if rc == 0 else rc
    finally:
        cli.close()


if __name__ == "__main__":
    raise SystemExit(main())