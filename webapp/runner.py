#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务执行器：每个任务一个线程，逐视频推进并回报真实进度。

要点：
1. `Fucker` 只吃 cookies，所以每个用户各跑各的会话，互不干扰；
2. 智到课程用 `getZhidaoContext` 拿到每节视频的 `watchState`，
   再逐节调用 `fuckZhidaoVideo`，因此 total/done 是真实数字而非估算；
3. 所有 print 被重定向到线程私有 sink，于是网页里能看实时日志。
"""
import io
import json
import os
import re
import sys
import threading
import time
import traceback
from urllib.parse import unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fucker import Fucker, CaptchaException, TimeLimitExceeded  # noqa: E402
from ObjDict import ObjDict  # noqa: E402
from logger import logger  # noqa: E402
from utils import cookie_jar_to_list, list_to_cookie_jar  # noqa: E402

import ai  # noqa: E402
import store  # noqa: E402

MAX_CONCURRENT = int(os.environ.get("ZHS_MAX_CONCURRENT", "3"))
_tls = threading.local()


# ---------------- 单 IP 风控对抗 ----------------
#
# 智慧树的风控看的不只是请求频率，更看"这些账号是不是同一个人"。
# 原版 Fucker 给所有会话写死同一个 UA（Chrome 100 / macOS / en-GB），
# 同一个 IP 上几十个"不同用户"指纹完全一致，是最容易被关联的特征。
# 这里按账号邮箱做稳定散列选一套指纹：同一账号每次跑都一样（不会自相矛盾），
# 不同账号之间彼此不同。

def _build_profiles():
    """拼一批真实存在过的桌面浏览器指纹。

    版本号跨了 14 个 Chrome 大版本、Windows 与 macOS 两种平台、
    加上 Edge / Firefox / Safari，指纹池足够大，几十个账号不会撞成同一张脸。
    """
    p = []
    win = "Windows NT 10.0; Win64; x64"
    mac = "Macintosh; Intel Mac OS X 10_15_7"
    langs = ["zh-CN,zh;q=0.9", "zh-CN,zh;q=0.9", "zh-CN,zh;q=0.8,en;q=0.6"]
    for i, v in enumerate(range(120, 134)):
        host = win if i % 3 != 2 else mac
        plat = "Windows" if host is win else "macOS"
        p.append((
            f"Mozilla/5.0 ({host}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{v}.0.0.0 Safari/537.36",
            f'"Google Chrome";v="{v}", "Chromium";v="{v}", "Not_A Brand";v="24"',
            plat, langs[i % len(langs)],
        ))
    for v in (131, 129):
        p.append((
            f"Mozilla/5.0 ({win}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{v}.0.0.0 Safari/537.36 Edg/{v}.0.0.0",
            f'"Microsoft Edge";v="{v}", "Chromium";v="{v}", "Not_A Brand";v="24"',
            "Windows", "zh-CN,zh;q=0.9",
        ))
    for v in (128.0, 131.0):
        p.append((
            f"Mozilla/5.0 ({win}; rv:{v}) Gecko/20100101 Firefox/{v}",
            "", "Windows", "zh-CN,zh;q=0.8,en-US;q=0.5",
        ))
    p.append((
        f"Mozilla/5.0 ({mac}) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.4 Safari/605.1.15", "", "macOS", "zh-CN,zh;q=0.9"))
    p.append((
        f"Mozilla/5.0 ({mac}) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/16.6 Safari/605.1.15", "", "macOS", "zh-CN,zh;q=0.9"))
    return p


_UA_PROFILES = _build_profiles()


def _profile_for(seed: str) -> dict:
    """按账号取一套稳定的浏览器指纹。"""
    import hashlib
    h = hashlib.sha256((seed or "anon").encode("utf-8")).digest()
    ua, ch_ua, platform, lang = _UA_PROFILES[h[0] % len(_UA_PROFILES)]
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": lang,
        "User-Agent": ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{platform}"',
    }
    if ch_ua:
        headers["sec-ch-ua"] = ch_ua
    return headers


class _Risk:
    """单 IP 风控闸门：撞码后全站冷却 + 启动错峰。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._cooldown_until = 0.0
        self._hits = []          # 最近一小时的撞码时间戳
        self._last_captcha = 0.0
        self._load()

    def _load(self):
        """从数据库恢复冷却与撞码记录——否则重启一次闸门就形同虚设。"""
        try:
            self._cooldown_until = float(store.get_setting("risk_cooldown_until", "0") or 0)
        except Exception:
            self._cooldown_until = 0.0
        try:
            raw = json.loads(store.get_setting("risk_hits", "[]") or "[]")
            now = time.time()
            self._hits = [float(t) for t in raw if now - float(t) < 3600]
        except Exception:
            self._hits = []

    def _save(self):
        try:
            # 用单键写入：set_settings 只接受 DEFAULT_SETTINGS 白名单里的键
            store.set_setting("risk_cooldown_until", str(int(self._cooldown_until)))
            store.set_setting("risk_hits", json.dumps([int(t) for t in self._hits]))
        except Exception:
            pass

    def _cfg(self, key, default):
        try:
            return int(store.get_setting(key, default))
        except Exception:
            return default

    def cooldown_left(self) -> int:
        with self._lock:
            return max(0, int(self._cooldown_until - time.time()))

    def report_captcha(self):
        now = time.time()
        mins = max(0, self._cfg("captcha_cooldown", 8))
        with self._lock:
            self._last_captcha = now
            self._hits = [t for t in self._hits if now - t < 3600] + [now]
            if mins:
                self._cooldown_until = max(self._cooldown_until, now + mins * 60)
            n = len(self._hits)
            left = max(0, int(self._cooldown_until - now))
            self._save()
        logger.warning(f"风控：撞到验证码，冷却 {mins} 分钟（近一小时第 {n} 次）")
        return {"cooldown_left": left, "hits_1h": n}

    def clear(self):
        """管理员手动解除冷却。"""
        with self._lock:
            self._cooldown_until = 0.0
            self._hits = []
            self._save()

    def wait_turn(self, stop=None, sink=None):
        """启动前等冷却结束，再随机错峰，避免整批任务同一秒开打。"""
        logged = False
        while True:
            left = self.cooldown_left()
            if left <= 0:
                break
            if stop is not None and stop.is_set():
                return False
            if sink and not logged:
                human = (f"{left // 60} 分 {left % 60} 秒" if left >= 60 else f"{left} 秒")
                # 注意：sink 是 _LineSink.write，它自己会加时间戳前缀
                sink(f"风控冷却中，还需 {human}\n")
                logged = True
            # 1 秒粒度，保证停止信号能立刻生效
            time.sleep(1)
        stagger = max(0, self._cfg("stagger_max", 45))
        if stagger:
            import random
            d = random.uniform(0, stagger)
            if sink:
                sink(f"错峰等待 {d:.0f} 秒后开始\n")
            end = time.time() + d
            while time.time() < end:
                if stop is not None and stop.is_set():
                    return False
                time.sleep(0.5)
        return True

    def stats(self) -> dict:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits if now - t < 3600]
            return {
                "hits_1h": len(hits),
                "cooldown_left": max(0, int(self._cooldown_until - now)),
                "last": int(self._last_captcha) if self._last_captcha else 0,
            }


_risk = _Risk()


def _attr(obj, name, default=None):
    try:
        v = getattr(obj, name, None)
        return default if v is None else v
    except Exception:
        return default


def _pick(obj, *keys, default=""):
    for k in keys:
        v = _attr(obj, k, None)
        if v not in (None, ""):
            return v
        try:
            v = obj.get(k) if hasattr(obj, "get") else None
        except Exception:
            v = None
        if v not in (None, ""):
            return v
    return default


class SmartFucker(Fucker):
    """在原生 Fucker 上加一层 AI 兜底答题。

    智慧树弹题通常直接下发正确答案（`questionOptions[].result == '1'`），
    这种情况下不消耗任何 token；只有答案缺失时才问 DeepSeek。
    """

    def __init__(self, *args, answerer=None, **kwargs):
        self.answerer = answerer
        self.ai_answered = 0
        self.ai_missed = 0
        super().__init__(*args, **kwargs)

    def answerZhidao(self, q):
        q = ObjDict(q)
        opts = list(_attr(q, "questionOptions", []) or [])
        known = [str(_attr(o, "id")) for o in opts
                 if str(_attr(o, "result", "") or "") == "1"]
        if known:
            return ",".join(known)

        if not (self.answerer and self.answerer.available()):
            logger.warning("弹题未下发答案，且未配置 AI 答题，本题跳过")
            self.ai_missed += 1
            return ""

        stem = ""
        for k in ("testQuestionName", "questionName", "content",
                  "questionContent", "name"):
            v = _attr(q, k, None)
            if v:
                stem = re.sub(r"<[^>]+>", "", str(v)).strip()
                break
        options = [{"id": _attr(o, "id"), "content": _attr(o, "content", "")}
                   for o in opts]
        ids = self.answerer.answer(stem, options)
        # 二次校验：只接受真实存在于选项里的 id（防模型幻觉）
        valid = {str(o["id"]) for o in options}
        ids = [i for i in ids if str(i) in valid]
        if ids:
            self.ai_answered += 1
            logger.info(f"AI 兜底答对: {ids}")
            return ",".join(ids)
        self.ai_missed += 1
        logger.warning("AI 未给出可用答案，本题跳过")
        return ""


class _Pool:
    """按数据库里的 max_concurrent 动态限流，改配置即时生效。"""

    def __init__(self):
        self._cv = threading.Condition()
        self._active = 0

    def _limit(self) -> int:
        try:
            return max(1, int(store.get_setting("max_concurrent", MAX_CONCURRENT)))
        except Exception:
            return MAX_CONCURRENT

    def state(self):
        """(占用数, 上限)——用来区分「风控冷却」和「单纯排队等槽」。"""
        with self._cv:
            return self._active, self._limit()

    def acquire(self, stop=None) -> bool:
        with self._cv:
            while self._active >= self._limit():
                if stop is not None and stop.is_set():
                    return False
                self._cv.wait(timeout=5)
            self._active += 1
            return True

    def release(self):
        with self._cv:
            self._active = max(0, self._active - 1)
            self._cv.notify_all()


# ---------------- stdout 路由（线程私有） ----------------
class _Router(io.TextIOBase):
    def __init__(self, real):
        self._real = real

    @property
    def encoding(self):
        return "utf-8"

    def write(self, s):
        sink = getattr(_tls, "sink", None)
        if sink is not None:
            try:
                sink(s)
            except Exception:
                pass
            return len(s)
        try:
            return self._real.write(s)
        except Exception:
            return len(s)

    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass

    def isatty(self):
        return False


def _install_router():
    if not isinstance(sys.stdout, _Router):
        sys.stdout = _Router(sys.stdout)
    if not isinstance(sys.stderr, _Router):
        sys.stderr = _Router(sys.stderr)


_install_router()


class _LineSink:
    """把 \r 进度条与 \n 普通输出都切成可读日志行。"""

    def __init__(self, task_id):
        self.task_id = task_id
        self.buf = ""
        self.last = time.time()

    def write(self, s):
        self.buf += s
        while True:
            idx_n = self.buf.find("\n")
            idx_r = self.buf.find("\r")
            idx = min([i for i in (idx_n, idx_r) if i >= 0], default=-1)
            if idx < 0:
                break
            line = self.buf[:idx].strip()
            self.buf = self.buf[idx + 1:]
            if not line:
                continue
            # [VP] 百分比 已播秒 总秒 —— 只更新进度条字段，不写日志，否则每十几秒刷一行
            if line.startswith("[VP]"):
                try:
                    _, pct, cur, tot = line.split(" ", 3)
                    store.update_task(
                        self.task_id,
                        vprog=f"{float(pct):.1f}|{int(float(cur))}|{int(float(tot))}")
                except Exception:
                    pass
                continue
            store.append_log(self.task_id, time.strftime("%H:%M:%S ") + line)

    def tail(self):
        if self.buf.strip():
            store.append_log(self.task_id, time.strftime("%H:%M:%S ") + self.buf.strip())
            self.buf = ""


# ---------------- 任务注册表 ----------------
_registry = {}
_reg_lock = threading.Lock()
_slots = _Pool()


def is_running(task_id: str) -> bool:
    with _reg_lock:
        rec = _registry.get(task_id)
        return bool(rec and not rec["done"].is_set())


def stop_task(task_id: str) -> bool:
    with _reg_lock:
        rec = _registry.get(task_id)
    if not rec:
        return False
    rec["stop"].set()
    return True


def start_task(task_id: str):
    with _reg_lock:
        if task_id in _registry and not _registry[task_id]["done"].is_set():
            return
        rec = {"stop": threading.Event(), "done": threading.Event()}
        _registry[task_id] = rec
    threading.Thread(target=_guard, args=(task_id, rec), daemon=True).start()


def _guard(task_id, rec):
    task = store.get_task(task_id)
    store.update_task(task_id, status="queued")
    sink = _LineSink(task_id)
    _tls.sink = sink.write
    try:
        # 先等风控冷却和错峰，再抢并发槽。
        # 顺序不能反：等冷却的任务如果先占着槽，3 个槽被 3 个「等冷却」占满，
        # 后面的任务会永远排在 queued 里——这正是「任务一直卡在冷却中」的成因。
        #
        # 状态要跟真实原因对齐：真在等冷却才是 throttled（UI 显示「风控冷却」），
        # 只是排队等并发槽就是 queued（「排队中」）。以前无条件写 throttled，
        # 于是并发槽满了也会被显示成风控冷却，用户白等冷却、还以为是撞码了。
        store.update_task(
            task_id, status="throttled" if _risk.cooldown_left() > 0 else "queued")
        if not _risk.wait_turn(rec["stop"], sink.write):
            store.update_task(task_id, status="stopped")
            return
        if rec["stop"].is_set():
            store.update_task(task_id, status="stopped")
            return

        active, limit = _slots.state()
        if active >= limit:
            sink(f"全站并发已满（{active}/{limit}），排队等空位；这不是风控，不用等冷却\n")
            store.update_task(task_id, status="queued")

        got = _slots.acquire(rec["stop"])   # 全局限流，避免把出口 IP 打爆
        if not got:
            store.update_task(task_id, status="stopped")
            return
        try:
            store.update_task(task_id, status="running")
            _run(task, rec, sink)
        except Exception as e:
            store.update_task(
                task_id, status="failed",
                error=f"{type(e).__name__}: {e}",
            )
            store.append_log(task_id, time.strftime("%H:%M:%S ") + traceback.format_exc(limit=3))
        finally:
            _slots.release()
    finally:
        sink.tail()
        _tls.sink = None
        rec["done"].set()


def _run(task, rec, sink):
    tid = task["id"]
    f = make_fucker(task["user_id"])
    store.append_log(tid, f"{time.strftime('%H:%M:%S')} 指纹：UA "
                          f"{f.headers.get('User-Agent', '')[:58]}… | 倍速 {f.speed}")
    # 免费/廉价代理会随时死掉，而按账号哈希分配意味着同一个死代理会一直分给同一个人。
    # 开跑前探一次活：不通就改直连（不让任务因为一条死代理直接 failed），
    # 并把它从池子里摘掉，免得继续坑下一个账号。
    px = getattr(f, "proxies", None) or {}
    if px:
        purl = px.get("https") or px.get("http")
        if not _proxy_alive(px):
            store.append_log(tid, f"{time.strftime('%H:%M:%S')} 代理 {purl} 不通，"
                                  f"本次任务改直连，并把该代理移出池子")
            f.proxies = {}
            left = _drop_proxy(purl)
            if left >= 0:
                store.append_log(tid, f"{time.strftime('%H:%M:%S')} 代理池剩余 {left} 条")
    kind = task["kind"]
    cid = task["course_id"]

    if kind == "zhidao":
        _run_zhidao(f, tid, cid, rec)
    elif kind == "ai":
        _run_ai(f, tid, cid, rec)
    else:
        store.update_task(tid, total=None)
        store.append_log(tid, f"{time.strftime('%H:%M:%S')} 校内课整课模式，进度按课程整体推进")
        f.fuckCourse(cid)
        store.update_task(tid, status="stopped" if rec["stop"].is_set() else "done")


def _log_captcha_payload(tid, e):
    """把 code -12 的原始响应写进任务日志——判断能不能自动过码，全靠这份证据。"""
    raw = getattr(e, "response", None)
    url = getattr(e, "url", None)
    if raw is None:
        store.append_log(tid, f"{time.strftime('%H:%M:%S')} （未拿到服务端原始响应，fucker.py 版本偏旧）")
        return
    try:
        text = json.dumps(raw, ensure_ascii=False, default=str)
    except Exception:
        text = str(raw)
    store.append_log(tid, f"{time.strftime('%H:%M:%S')} 验证响应 {url or ''}: {text[:1200]}")
    # 顺手把可疑字段单独拎出来，方便一眼看出有没有可解的 challenge
    if isinstance(raw, dict):
        keys = ("img", "image", "captcha", "challenge", "gt", "token", "verify",
                "verifyType", "type", "uuid", "ticket", "url", "slice", "bg")
        hit = {k: v for k, v in raw.items() if k.lower() in [x.lower() for x in keys]}
        if hit:
            store.append_log(
                tid, f"{time.strftime('%H:%M:%S')} 可疑验证字段: "
                     f"{json.dumps(hit, ensure_ascii=False, default=str)[:800]}")


def _run_ai(f, tid, cid, rec):
    """AI 课：考点测验/试卷由 ExamCtx 调 DeepSeek 作答。"""
    try:
        course_id, class_id = cid.split("|", 1)
        course_id, class_id = int(course_id), int(class_id)
    except Exception:
        raise ValueError(f"AI 课 ID 格式应为 courseId|classId，收到：{cid}")

    cfg = ai.ai_config_for_fucker()
    if cfg.get("enabled"):
        store.append_log(tid, f"{time.strftime('%H:%M:%S')} AI 答题已启用：{ai.load()['model']}")
    else:
        store.append_log(tid, f"{time.strftime('%H:%M:%S')} 未配置 AI，测验会跳过（可回填历史答案）")

    store.update_task(tid, total=None)
    status = "stopped" if rec["stop"].is_set() else "done"
    try:
        f.fuckAiCourse(course_id, class_id, cfg, no_exam=False)
    except CaptchaException as e:
        status = "need_captcha"
        st = _risk.report_captcha()
        store.append_log(tid, f"{time.strftime('%H:%M:%S')} AI 课触发验证码"
                              f"（全站冷却 {st['cooldown_left'] // 60} 分钟）")
        _log_captcha_payload(tid, e)
    store.update_task(tid, status=status)
    if getattr(f, "ai_answered", 0):
        store.append_log(tid, f"{time.strftime('%H:%M:%S')} AI 兜底共作答 {f.ai_answered} 题")


def _run_zhidao(f, tid, cid, rec):
    ctx = f.getZhidaoContext(cid, force=True)
    videos = []
    for chapter in ctx.chapters.videoChapterDtos:
        for lesson in chapter.videoLessons:
            for v in lesson.videoSmallLessons:
                videos.append(v)
    total = len(videos)
    done = sum(1 for v in videos if getattr(v, "watchState", 0) == 1)
    store.update_task(tid, total=total, done=done)
    store.append_log(tid, f"{time.strftime('%H:%M:%S')} 共 {total} 节，已完成 {done} 节")

    status = "done"
    for v in videos:
        if rec["stop"].is_set():
            status = "stopped"
            break
        if getattr(v, "watchState", 0) == 1:
            continue
        store.append_log(tid, f"{time.strftime('%H:%M:%S')} ▶ {v.name}")
        try:
            store.update_task(tid, vprog="")   # 换节了，清掉上一节的进度
            f.fuckZhidaoVideo(cid, v.videoId)
            done += 1
            store.update_task(tid, done=done)
            store.append_log(tid, f"{time.strftime('%H:%M:%S')} ✔ {v.name}")
        except CaptchaException as e:
            status = "need_captcha"
            st = _risk.report_captcha()
            store.append_log(
                tid,
                f"{time.strftime('%H:%M:%S')} 需要验证码，任务中止"
                f"（全站冷却 {st['cooldown_left'] // 60} 分钟，近一小时第 {st['hits_1h']} 次）")
            _log_captcha_payload(tid, e)
            break
        except TimeLimitExceeded:
            status = "done"
            break
        except Exception as e:
            store.append_log(tid, f"{time.strftime('%H:%M:%S')} ✘ {v.name}: {e}")
    store.update_task(tid, status=status, done=done)


# ---------------- 绑定 / 课程 ----------------
def risk_status() -> dict:
    """风控状态：近一小时撞码次数、剩余冷却秒数。"""
    return _risk.stats()


def _load_cookies(user_id):
    import sechelp
    row = store.get_zhs(user_id)
    if not row:
        raise RuntimeError("尚未绑定智慧树账号")
    return json.loads(sechelp.decrypt(row["cookies"]))


def _seed_of(user_id) -> str:
    try:
        u = store.get_user(user_id)
        return (u or {}).get("email") or str(user_id)
    except Exception:
        return str(user_id)


def _speed_for(seed: str) -> float:
    """每个账号固定一个播放倍速，避免全站整齐划一。
    上限压到 1.4：真人看课极少长期 1.5 倍速，倍速越高越容易触发风控。"""
    import hashlib
    import random
    h = hashlib.sha256(("speed:" + (seed or "anon")).encode()).digest()
    return round(random.Random(h[:8]).uniform(1.15, 1.4), 2)


def proxy_pool() -> list:
    """从设置里读代理池；每行或逗号分隔一条，形如 http://user:pass@ip:port。"""
    raw = (store.get_setting("proxy_pool", "") or "").strip()
    if not raw:
        return []
    items = []
    for chunk in raw.replace("\r", "\n").replace(";", ",").split("\n"):
        for p in chunk.split(","):
            p = p.strip()
            if p:
                items.append(p)
    return items


def _proxy_for(seed: str) -> dict:
    """按账号哈希固定分配代理——同一账号始终走同一出口 IP，换 IP 反而更可疑。"""
    pool = proxy_pool()
    if not pool:
        return {}
    import hashlib
    h = hashlib.sha256(("proxy:" + (seed or "anon")).encode()).digest()
    p = pool[int.from_bytes(h[:4], "big") % len(pool)]
    return {"http": p, "https": p}


PROBE_URL = "https://www.zhihuishu.com/"


def _proxy_alive(proxies: dict, timeout: float = 6.0) -> bool:
    """探一次活：只有能真正走通 HTTPS CONNECT 到智慧树的代理才算可用。"""
    try:
        import requests
        r = requests.get(PROBE_URL, proxies=proxies, timeout=timeout,
                         allow_redirects=False)
        return r.status_code < 500
    except Exception:
        return False


def _drop_proxy(url: str) -> int:
    """把一条死代理从 settings.proxy_pool 里摘掉，返回剩余条数（-1 表示没动成）。"""
    if not url:
        return -1
    try:
        raw = store.get_setting("proxy_pool", "") or ""
        lines = [ln.strip() for ln in raw.replace(",", "\n").split("\n") if ln.strip()]
        keep = [ln for ln in lines if ln != url]
        if len(keep) == len(lines):
            return len(keep)
        store.set_setting("proxy_pool", "\n".join(keep))
        logger.warning(f"代理 {url} 探活失败，已从池中移除，剩 {len(keep)} 条")
        return len(keep)
    except Exception:
        return -1


def make_fucker(user_id) -> Fucker:
    seed = _seed_of(user_id)
    return SmartFucker(cookies=_load_cookies(user_id), proxies=_proxy_for(seed),
                       headers=_profile_for(seed),
                       speed=_speed_for(seed),
                       tree_view=False, progressbar_view=False,
                       answerer=ai.Answerer())


def fetch_courses(user_id):
    f = make_fucker(user_id)
    out = {"zhidao": [], "hike": [], "ai": []}
    try:
        for c in f.getZhidaoList() or []:
            out["zhidao"].append({
                "id": c.secret,
                "name": _pick(c, "courseName") or "（未命名）",
                "meta": _pick(c, "teacherName"),
            })
    except Exception as e:
        out["zhidao_error"] = str(e)
    try:
        for c in f.getHikeList() or []:
            out["hike"].append({
                "id": str(c.courseId),
                "name": _pick(c, "courseName") or "（未命名）",
                "meta": _pick(c, "teacherName"),
            })
    except Exception as e:
        out["hike_error"] = str(e)
    try:
        for c in f.getZhidaoAiList() or []:
            cid, kid = _pick(c, "courseId"), _pick(c, "classId")
            if cid and kid:
                out["ai"].append({
                    "id": f"{cid}|{kid}",
                    "name": _pick(c, "courseName") or "（未命名 AI 课）",
                    "meta": _pick(c, "teacherName"),
                })
    except Exception as e:
        out["ai_error"] = str(e)
    return out


# ---------------- 二维码绑定会话 ----------------
QR_SESSIONS = {}
_qr_lock = threading.Lock()
QR_TTL = 300

LOGIN_PAGE = "https://passport.zhihuishu.com/login"
QR_IMG = "https://passport.zhihuishu.com/qrCodeLogin/getLoginQrImg"
QR_INFO = "https://passport.zhihuishu.com/qrCodeLogin/getLoginQrInfo"
LOGIN_SERVICE = "https://onlineservice-api.zhihuishu.com/login/gologin"


def qr_start(user_id: int) -> str:
    import secrets
    sid = secrets.token_urlsafe(16)
    with _qr_lock:
        QR_SESSIONS[sid] = {"user_id": user_id, "status": "starting",
                            "msg": "正在获取二维码", "img": None, "t": time.time()}
    threading.Thread(target=_qr_worker, args=(sid,), daemon=True).start()
    return sid


def qr_get(sid: str):
    with _qr_lock:
        s = QR_SESSIONS.get(sid)
        return dict(s) if s else None


def _qr_set(sid, **kw):
    with _qr_lock:
        if sid in QR_SESSIONS:
            QR_SESSIONS[sid].update(kw)


def _qr_cleanup():
    now = time.time()
    with _qr_lock:
        for k in [k for k, v in QR_SESSIONS.items() if now - v["t"] > QR_TTL + 120]:
            QR_SESSIONS.pop(k, None)


def _qr_worker(sid):
    import sechelp
    user_id = QR_SESSIONS[sid]["user_id"]
    try:
        f = Fucker(cookies={}, proxies={}, tree_view=False, progressbar_view=False)
        f._sessionReady()
        r = f.session.get(QR_IMG, timeout=15).json()
        qr_token = r["qrToken"]
        _qr_set(sid, status="waiting", img=r["img"], msg="用「知到 / 智慧树」App 扫码")

        deadline = time.time() + QR_TTL
        while time.time() < deadline:
            time.sleep(1.2)
            try:
                info = f.session.get(QR_INFO, params={"qrToken": qr_token}, timeout=15).json()
            except Exception:
                continue
            st = info.get("status")
            if st == 0:
                _qr_set(sid, status="scanned", msg="已扫码，请在手机上确认登录")
            elif st == 1:
                f.session.get(LOGIN_PAGE, params={"service": LOGIN_SERVICE,
                                                  "pwd": info.get("oncePassword")}, timeout=15)
                f.cookies = f.session.cookies.copy()
                cookie_list = cookie_jar_to_list(f.cookies)
                label = _guess_label(f)
                store.save_zhs(user_id, label, sechelp.encrypt(json.dumps(cookie_list)))
                _qr_set(sid, status="success", msg=f"绑定成功：{label}")
                return
            elif st == 2:
                _qr_set(sid, status="expired", msg="二维码已过期，请重新获取")
                return
            elif st == 3:
                _qr_set(sid, status="failed", msg="已取消登录")
                return
        _qr_set(sid, status="expired", msg="等待超时，请重新获取")
    except Exception as e:
        _qr_set(sid, status="failed", msg=f"绑定失败：{e}")
    finally:
        _qr_cleanup()


def _guess_label(f) -> str:
    """尽量从 CASLOGC 里读出昵称/账号，读不到就用 uuid 尾号。"""
    for key in ("CASLOGC", "caslogc"):
        raw = f.cookies.get(key)
        if not raw:
            continue
        try:
            data = json.loads(unquote(raw))
        except Exception:
            continue
        for k in ("realName", "nickName", "userName", "username", "account"):
            if data.get(k):
                return str(data[k])
        if data.get("uuid"):
            return "尾号" + str(data["uuid"])[-4:]
    return "已绑定账号"