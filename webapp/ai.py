#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek 答题器（OpenAI 兼容协议）。

用途：智慧树给「弹题」下发答案时直接选，未下发时用这里兜底；
AI 课的考点测验/试卷（ExamCtx）也走同一份配置。
"""
import base64
import json
import re
import time

import requests

import sechelp
import store

DEFAULTS = {
    "enabled": False,
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "api_key": "",
    "timeout": 60,
}

_SETTING_KEY = "ai_config"

# 明确不走系统代理：本机回环请求被代理吞掉会直接挂到超时
_SESSION = requests.Session()
_SESSION.trust_env = False


def _norm_base(url: str) -> str:
    """统一成不含 /v1 的根地址：fucker 的 AIClient 会自己拼 /v1/chat/completions。"""
    url = (url or DEFAULTS["base_url"]).strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url


def load() -> dict:
    raw = store.get_setting(_SETTING_KEY, "")
    if not raw:
        return dict(DEFAULTS)
    try:
        data = json.loads(raw)
    except Exception:
        return dict(DEFAULTS)
    key = ""
    if data.get("api_key_enc"):
        try:
            key = sechelp.decrypt(base64.b64decode(data["api_key_enc"]))
        except Exception:
            key = ""
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    cfg["api_key"] = key
    cfg["base_url"] = _norm_base(cfg.get("base_url"))
    return cfg


def save(patch: dict) -> dict:
    """patch 里 api_key 为空字符串表示「不修改已保存的密钥」。"""
    cur = load()
    cfg = dict(cur)
    for k, v in patch.items():
        if k in DEFAULTS and k != "api_key":
            cfg[k] = v
    if patch.get("api_key"):
        cfg["api_key"] = str(patch["api_key"]).strip()
    if "clear_key" in patch and patch["clear_key"]:
        cfg["api_key"] = ""
    cfg["base_url"] = _norm_base(cfg.get("base_url"))
    try:
        cfg["timeout"] = max(10, min(180, int(cfg.get("timeout") or 60)))
    except Exception:
        cfg["timeout"] = 60

    blob = {
        "enabled": bool(cfg["enabled"]),
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "timeout": cfg["timeout"],
        "api_key_enc": base64.b64encode(sechelp.encrypt(cfg["api_key"])).decode()
        if cfg["api_key"] else "",
    }
    store.set_setting(_SETTING_KEY, json.dumps(blob))
    return cfg


def public_view() -> dict:
    """给前端的脱敏视图：不回传密钥明文。"""
    cfg = load()
    return {
        "enabled": bool(cfg["enabled"]),
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "has_key": bool(cfg["api_key"]),
        "key_hint": ("sk-…" + cfg["api_key"][-4:]) if len(cfg["api_key"]) > 8 else "",
    }


def ai_config_for_fucker() -> dict:
    """fucker 的 ExamCtx 需要的结构。"""
    cfg = load()
    if not (cfg["enabled"] and cfg["api_key"]):
        return {"enabled": False}
    return {
        "enabled": True,
        "use_zhidao_ai": False,
        "use_stream": False,
        "openai": {
            "api_base": _norm_base(cfg["base_url"]),
            "api_key": cfg["api_key"],
            "model_name": cfg["model"],
        },
    }


class Answerer:
    """把一道选择题丢给 DeepSeek，取回选项 id 列表。"""

    def __init__(self, cfg: dict = None):
        self.cfg = cfg or load()
        self.calls = 0
        self.fallbacks = 0

    def available(self) -> bool:
        return bool(self.cfg.get("enabled") and self.cfg.get("api_key"))

    def chat(self, prompt: str) -> str:
        url = _norm_base(self.cfg["base_url"]) + "/v1/chat/completions"
        body = {
            "model": self.cfg["model"],
            "messages": [
                {"role": "system", "content": "你是一个中文考试答题器。只输出题目要求的 JSON，不输出任何解释。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "stream": False,
        }
        headers = {
            "Authorization": "Bearer " + self.cfg["api_key"],
            "Content-Type": "application/json",
        }
        last = None
        for attempt in range(3):
            try:
                r = _SESSION.post(url, headers=headers, json=body,
                                  timeout=self.cfg.get("timeout", 60))
                r.raise_for_status()
                self.calls += 1
                return r.json()["choices"][0]["message"]["content"]
            except Exception as e:
                last = e
                time.sleep(1.2 * (attempt + 1))
        raise RuntimeError(f"DeepSeek 调用失败: {last}")

    @staticmethod
    def _extract_ids(text: str, valid_ids: set) -> list:
        """从模型输出里挖出选项 id，兼容 ```answer 块 / 裸 JSON / 纯数字列表。"""
        candidates = []
        block = re.search(r"```(?:answer|json)?\s*(.*?)```", text, re.DOTALL)
        chunks = [block.group(1)] if block else []
        chunks.append(text)
        arr = re.search(r"\[[^\[\]]*\]", text, re.DOTALL)
        if arr:
            chunks.append(arr.group(0))
        for chunk in chunks:
            for m in re.finditer(r"\{[^{}]*\}|\[[^\[\]]*\]|\b\d{3,}\b", chunk, re.DOTALL):
                seg = m.group(0)
                if seg.startswith(("[", "{")):
                    try:
                        data = json.loads(seg)
                    except Exception:
                        continue
                    items = data if isinstance(data, list) else data.get("answer", [])
                    if not isinstance(items, list):
                        items = [items]
                    for it in items:
                        if isinstance(it, dict):
                            it = it.get("id") or it.get("optionId")
                        if it is not None and str(it).strip() in valid_ids:
                            candidates.append(str(it).strip())
                else:
                    if seg in valid_ids:
                        candidates.append(seg)
            if candidates:
                break
        # 去重保序
        seen, out = set(), []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def answer(self, stem: str, options: list) -> list:
        """options: [{'id':..,'content':..}, ...] 返回选中的 id 字符串列表。"""
        if not self.available():
            return []
        valid = {str(o["id"]) for o in options}
        lines = []
        for o in options:
            content = re.sub(r"<[^>]+>", "", str(o.get("content", ""))).strip()
            lines.append(f"- id={o['id']} | {content}")
        prompt = (
            "下面是一道选择题，请选出正确答案。\n\n"
            f"题目：{stem}\n\n选项：\n" + "\n".join(lines) + "\n\n"
            '只输出 JSON：{"answer":[选项id,...]}。\n'
            "单选只给一个 id；多选给全部正确 id；判断题 id 同样从选项里取。"
        )
        try:
            raw = self.chat(prompt)
        except Exception:
            self.fallbacks += 1
            return []
        ids = self._extract_ids(raw, valid)
        if not ids:
            self.fallbacks += 1
        return ids