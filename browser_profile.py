#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""浏览器指纹画像 + 代理池 + stealth 注入脚本 + 代理认证扩展。

本模块只做纯逻辑/数据生成，不依赖 DrissionPage 运行时，方便单元测试。
主程序 grok_register_ttk.py 负责把这里生成的画像应用到 ChromiumOptions
并通过 CDP 注入 stealth 脚本。
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import tempfile
import threading
from urllib.parse import urlparse, unquote


# ============ 代理解析工具 ============

def parse_proxy(proxy):
    """解析代理串 -> dict(raw, scheme, host, port, username, password)；无效返回 None。"""
    p = str(proxy or "").strip()
    if not p:
        return None
    try:
        u = urlparse(p if "://" in p else f"http://{p}")
    except Exception:
        return None
    host = u.hostname or ""
    if not host:
        return None
    scheme = (u.scheme or "http").lower()
    try:
        port = u.port or (443 if scheme == "https" else 80)
    except Exception:
        port = 443 if scheme == "https" else 80

    def _dec(v):
        # userinfo 在 URL 里是百分号编码的，认证时要用解码后的真实值
        if not v:
            return ""
        try:
            return unquote(v)
        except Exception:
            return v

    return {
        "raw": p,
        "scheme": scheme,
        "host": host,
        "port": int(port),
        "username": _dec(u.username),
        "password": _dec(u.password),
    }


def proxy_server_arg(proxy):
    """返回 chromium --proxy-server 用的 scheme://host:port（不含认证）。"""
    info = parse_proxy(proxy)
    if not info:
        return ""
    return f"{info['scheme']}://{info['host']}:{info['port']}"


def proxy_has_auth(proxy):
    info = parse_proxy(proxy)
    return bool(info and info["username"])


def proxy_log_label(proxy):
    """脱敏后的代理标签，用于日志。"""
    info = parse_proxy(proxy)
    if not info:
        return "(none)"
    auth = "user:***@" if info["username"] else ""
    return f"{info['scheme']}://{auth}{info['host']}:{info['port']}"


def normalize_proxy_list(value):
    """接受 list/tuple 或 逗号/换行/空格分隔字符串 -> 去重保序的代理列表。"""
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = re.split(r"[,\n\r]+", str(value or ""))
    out = []
    seen = set()
    for it in items:
        s = str(it).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ============ 代理池 ============

class ProxyPool:
    """管理单一代理或代理池的选取、轮换、失效标记与并发占用（线程安全）。"""

    def __init__(self, mode="single", single=None, pool=None, strategy="rotate"):
        self.mode = "pool" if str(mode or "").lower() == "pool" else "single"
        self.single = str(single or "").strip()
        self.pool = normalize_proxy_list(pool or [])
        self.strategy = "random" if str(strategy or "").lower() == "random" else "rotate"
        self._idx = 0
        self._bad = set()
        self._in_use = set()
        self._cond = threading.Condition()

    def size(self):
        if self.mode == "pool":
            return len(self.pool)
        return 1 if self.single else 0

    def ip_budget(self, max_ip_retry):
        """单账号允许尝试的 IP 个数上限。"""
        if self.mode != "pool":
            return 1
        try:
            cap = int(max_ip_retry or 1)
        except Exception:
            cap = 1
        return max(1, min(len(self.pool) or 1, cap))

    def _available(self):
        return [p for p in self.pool if p not in self._bad]

    def _pick_locked(self, exclude_in_use):
        """锁内挑一个代理；exclude_in_use 时跳过占用中的。无可用返回 None。"""
        avail = [
            p
            for p in self.pool
            if p not in self._bad and (not exclude_in_use or p not in self._in_use)
        ]
        if not avail:
            return None
        if self.strategy == "random":
            return random.choice(avail)
        proxy = avail[self._idx % len(avail)]
        self._idx += 1
        return proxy

    def acquire(self, cancel=None, timeout_per_wait=1.0):
        """领取一个代理。

        pool 模式排他（每个并发 worker 拿到不同、未失效的 IP；暂时无空闲则等待
        直到有人释放）。非 pool 模式返回单一代理（不排他，允许共享）。
        cancel 为可选回调，返回 True 时放弃等待并返回 None。
        """
        if self.mode != "pool":
            return self.single or None
        with self._cond:
            while True:
                if cancel and cancel():
                    return None
                proxy = self._pick_locked(exclude_in_use=True)
                if proxy is not None:
                    self._in_use.add(proxy)
                    return proxy
                if not self._available():
                    # 全部失效 → 重置后重试
                    self._bad.clear()
                    continue
                # 有效 IP 都被占用中 → 等待释放
                self._cond.wait(timeout=timeout_per_wait)

    def pick_next(self):
        """兼容旧接口：非排他地取下一个代理（不标记占用）。"""
        if self.mode != "pool":
            return self.single or None
        with self._cond:
            proxy = self._pick_locked(exclude_in_use=False)
            if proxy is None:
                self._bad.clear()
                proxy = self._pick_locked(exclude_in_use=False)
            return proxy

    def release(self, proxy):
        """释放占用的代理并唤醒等待者。"""
        if self.mode != "pool" or not proxy:
            return
        with self._cond:
            self._in_use.discard(proxy)
            self._cond.notify_all()

    def mark_bad(self, proxy):
        if not proxy:
            return
        with self._cond:
            self._bad.add(proxy)
            self._in_use.discard(proxy)
            self._cond.notify_all()

    def has_alternative(self):
        """代理池模式下是否还有未失效的可切换 IP。"""
        if self.mode != "pool":
            return False
        with self._cond:
            return len(self._available()) > 0


# ============ 指纹画像（自洽受限随机化） ============
#
# 重要：不伪造 UA / navigator.platform / WebGL / Canvas。
# 这些一旦与真实 OS、client hints、TLS/JA3 不一致，反而会被 Cloudflare Turnstile
# 判定为机器人。只随机"真实浏览器本就会变化、且不与其他信号冲突"的安全维度：
# 窗口视口、界面语言。真实 UA / 平台 / client hints 全部保持不变。

COMMON_VIEWPORTS = [
    [1920, 1080],
    [1680, 1050],
    [1600, 900],
    [1536, 864],
    [1440, 900],
    [1366, 768],
    [2560, 1440],
]

LANG_CHOICES = [
    ["en-US", "en"],
    ["en-GB", "en"],
    ["en-US", "en", "zh-CN"],
]


def _accept_language(languages):
    parts = []
    for i, lang in enumerate(languages):
        if i == 0:
            parts.append(lang)
        else:
            q = max(0.1, round(1 - i * 0.1, 1))
            parts.append(f"{lang};q={q}")
    return ",".join(parts)


def build_account_profile(proxy=None, rng=None):
    """生成一个账号的浏览器画像。

    只包含安全可随机的维度（视口、语言）与本账号选定的代理。
    刻意不含 UA / 平台 / WebGL / Canvas，避免与真实浏览器信号冲突。
    """
    rng = rng or random
    viewport = list(rng.choice(COMMON_VIEWPORTS))
    languages = list(rng.choice(LANG_CHOICES))
    return {
        "viewport": viewport,
        "languages": languages,
        "accept_language": _accept_language(languages),
        "lang": languages[0],
        "proxy": str(proxy or "").strip(),
        "ext_dir": None,  # 代理认证扩展临时目录，运行时填
    }


def profile_summary(profile):
    """一行摘要，用于日志。"""
    if not profile:
        return "(none)"
    return (
        f"viewport={profile.get('viewport')} lang={profile.get('lang')} "
        f"ip={proxy_log_label(profile.get('proxy'))}"
    )


# ============ 代理认证扩展（MV3） ============

def make_proxy_auth_extension(proxy):
    """带认证代理时生成临时 MV3 扩展目录，在 onAuthRequired 注入凭据。

    无认证或无效代理返回 None。返回的目录需在用完后删除（见 cleanup_dir）。
    """
    info = parse_proxy(proxy)
    if not info or not info["username"]:
        return None

    manifest = {
        "manifest_version": 3,
        "name": "px-auth",
        "version": "1.0.0",
        "permissions": ["webRequest", "webRequestAuthProvider"],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
        "minimum_chrome_version": "108",
    }
    background = (
        "chrome.webRequest.onAuthRequired.addListener(\n"
        "  function (details) {\n"
        "    return { authCredentials: { username: %s, password: %s } };\n"
        "  },\n"
        '  { urls: ["<all_urls>"] },\n'
        '  ["blocking"]\n'
        ");\n"
    ) % (json.dumps(info["username"]), json.dumps(info["password"]))

    ext_dir = tempfile.mkdtemp(prefix="pxauth_")
    with open(os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    with open(os.path.join(ext_dir, "background.js"), "w", encoding="utf-8") as f:
        f.write(background)
    return ext_dir


def cleanup_dir(path):
    if path and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def cleanup_profile(profile):
    """删除画像关联的临时扩展目录。"""
    if not profile:
        return
    cleanup_dir(profile.get("ext_dir"))
    profile["ext_dir"] = None


# ============ stealth 注入脚本 ============

def build_stealth_script(profile=None):
    """生成在每个新文档加载前执行的 stealth JS。

    只做与真实 Chrome 一致、不制造矛盾的最小改动：隐藏自动化痕迹
    (navigator.webdriver)、补齐 window.chrome、修正 permissions.query。
    刻意不改 platform / languages / hardwareConcurrency / WebGL，也不给
    Canvas 加噪 —— 那些会与真实 UA / client hints / TLS 冲突，触发 Turnstile。
    """
    return """
(() => {
  const defineGet = (obj, prop, val) => {
    try { Object.defineProperty(obj, prop, { get: () => val, configurable: true }); } catch (e) {}
  };
  try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}
  defineGet(navigator, 'webdriver', false);
  if (!window.chrome) { window.chrome = { runtime: {} }; }
  try {
    const orig = window.navigator.permissions.query.bind(window.navigator.permissions);
    window.navigator.permissions.query = (params) =>
      params && params.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : orig(params);
  } catch (e) {}
})();
"""
